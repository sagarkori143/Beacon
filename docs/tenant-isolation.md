# Tenant isolation

One rule: **tenant scope comes from the authenticated identity, never from a
request parameter.** Everything below exists to make that true even when a
handler is wrong.

There are four independent layers. Any one of them failing should not leak data.

---

## 1. Scope comes from the token

A JWT carries `org`, `loc`, `role` and a `tv` (token version). A
`Principal` is built from those claims and nothing else.

A caller may **narrow** within their scope — a front-desk user can ask about
their own property — but never widen it:

```python
TenantContext(ORG_A).narrowed_to(GINZA)         # fine: unpinned admin
TenantContext(ORG_A, GINZA).narrowed_to(GINZA)  # fine: restating their own
TenantContext(ORG_A, GINZA).narrowed_to(CHIYODA)# TenantScopeError
```

That last case raises rather than silently falling back to the caller's own
scope. A silent fallback is worse: the caller believes they received Chiyoda's
answer.

Any `organization_id` arriving in a request body is ignored, not honoured.

---

## 2. Row Level Security

Every tenant table has RLS **enabled and forced**, with one policy:

```sql
CREATE POLICY tenant_isolation ON chunks
    USING       (organization_id = nullif(current_setting('app.current_org_id', true), '')::uuid)
    WITH CHECK  (organization_id = nullif(current_setting('app.current_org_id', true), '')::uuid);
```

Three details are load-bearing.

**`FORCE`, not just `ENABLE`.** A table's owner is exempt from its own policies
unless forced. Without it, a deployment that happened to connect as the owner
would see every tenant's rows with no error anywhere.

**`nullif(..., '')`.** An unset setting is the empty string. Casting `''` to
`uuid` raises; comparing `NULL` denies. Denying is the only safe failure mode —
an error here would tempt someone to "fix" it by loosening the policy.

**`WITH CHECK` as well as `USING`.** Reads are not the only direction. Without
it, a bug that wrote the wrong `organization_id` would plant a row inside
someone else's tenant.

### The two-role split

| Role | Used by | Privileges |
|---|---|---|
| `app` (owner) | Migrations, tenant provisioning | Owns the schema. In the Compose image, also a superuser. |
| `app_rw` | **The application** | `NOSUPERUSER NOBYPASSRLS`, owns nothing. |

**A superuser bypasses RLS entirely and silently.** No error, no failed query —
every tenant simply sees everything. It is the single worst misconfiguration
available, and it is one careless `DATABASE_URL` away, so:

- `docker-compose.yml` points the api and worker at `app_rw`;
- a boot guard checks `pg_roles` and refuses to start in production
  (`app/core/db.py::check_application_role`);
- `/health/ready` reports it;
- an [integration test](../tests/integration/test_rls.py) asserts the test role
  itself cannot bypass RLS — otherwise every other isolation test would pass
  while proving nothing.

---

## 3. Transaction-local context

Tenant context is set as the **first statement inside an explicitly begun
transaction**:

```python
await session.execute(
    text("SELECT set_config('app.current_org_id', :org, true)"),  # is_local = true
    {"org": str(organization_id)},
)
```

`is_local=true` means the value is reverted when the transaction ends. The
alternative is a genuine cross-tenant read: a session-level `SET` persists on the
connection, that connection returns to the pool, and the next request — for a
different tenant — inherits it. `ROLLBACK` does not clear a session-level `SET`,
so pool recycling does not save you.

There is exactly one way to obtain a session, `tenant_session(org_id)`, and both
HTTP requests and background workers go through it. A
[test greps the source](../tests/unit/test_architecture.py) for
`set_config(..., false)` so the mistake cannot be introduced later.

---

## 4. Application-level filtering

Every query also carries `organization_id` as an explicit predicate. RLS already
enforces it; the predicate exists so the planner uses the tenant index instead of
filtering rows it has already fetched.

Location filtering is **only** at this layer. RLS is the organization boundary,
not the location one:

| Caller | Sees |
|---|---|
| Pinned to Ginza | Ginza's documents + organization-wide documents |
| No location | Organization-wide documents only |
| Admin, no location | Any location they explicitly ask for, plus org-wide |

There is deliberately no "search every location" fallback. That is exactly how
one property's private information reaches another property's guest.

---

## The one deliberate exception

Login must find a user before any organization is known. Something has to be
readable without tenant context.

Rather than punching a hole in the `users` policy — which would expose password
hashes, roles and names to an unscoped query — there is a separate, deliberately
tiny table:

```sql
user_directory (email PRIMARY KEY, organization_id, user_id, is_active)
```

Login resolves the organization from it, then reads the real user row inside a
properly scoped session. The exposure is an email-to-organization mapping and
nothing else.

`embedding_spaces` is also global: it is a registry of `(model, dimension)`
pairs and contains no tenant data. So is `platform_users` — see below.

All are listed in `app/models/__init__.py::GLOBAL_TABLES`, and an integration
test asserts that **no other table** lacks RLS — so nothing ends up outside
tenant scoping by accident.

---

## Platform operators

Someone has to create the tenants. That role cannot itself be a tenant user, so
`platform_users` is a second, separate credential type: accounts that belong to
no organization, authenticate at `/platform/auth/login`, and carry `"pt":
"platform"` in their token.

**The two token types are mutually exclusive, enforced in the decoder.**
`decode_token` rejects anything that is not a tenant token and
`decode_platform_token` rejects anything that is not a platform token — before
any permission check runs, so a carelessly wired dependency cannot let one stand
in for the other. A platform token is refused by every tenant endpoint; a tenant
token is refused by every `/platform` endpoint.

Creating a tenant needs one narrow widening, since the `organizations` policy
restricts a session to its own row and a brand-new organization is nobody's own
row yet. A second GUC handles it:

```sql
-- migrations/.../7a2f5c91b4e3
CREATE POLICY tenant_isolation ON organizations
    USING      (id = nullif(current_setting('app.current_org_id', true), '')::uuid
                OR current_setting('app.platform_admin', true) = 'on')
    WITH CHECK (... same ...);
```

`WITH CHECK` is what admits the INSERT; `USING` is what admits the listing.

**The widening covers `organizations` and nothing else.** Every tenant table
still keys on `app.current_org_id` alone, so `platform_session(org_id)` scopes a
platform operator to exactly one tenant, and `platform_session(None)` reads no
tenant table at all. The invariant survives intact:

> No credential in the system — tenant or platform — can read two organizations'
> data in one transaction.

That is why an operator can create an organization, its locations and its users,
but cannot read anyone's documents, chunks or conversations. To do that they
create themselves a user in that organization, which is an audited action and
leaves the invariant standing.

---

## What is tested

[`tests/integration/test_rls.py`](../tests/integration/test_rls.py) enumerates
the live schema rather than spot-checking it:

- the application role is not a superuser and cannot bypass RLS;
- every tenant table has RLS enabled *and* forced;
- every tenant table has exactly one policy (two permissive policies OR
  together, which is a quiet way to widen access);
- any table without RLS is in the documented exception list;
- a foreign organization reads zero rows; the owning one reads its own;
- no tenant context denies everything;
- a forged `organization_id` on insert is rejected;
- the setting does not survive its transaction.

[`tests/integration/test_retrieval.py`](../tests/integration/test_retrieval.py)
covers the guarantee end-to-end, in both directions: across organizations, and
across locations within one organization. The second is the easier one to get
wrong and the one a customer notices first.

[`tests/integration/test_platform.py`](../tests/integration/test_platform.py)
covers the operator boundary: that the two token types reject each other, that a
`platform_session` with no organization named reads zero rows from `users`,
`documents` and `chunks`, and that a tenant provisioned through the API is
isolated *once it holds knowledge* — it retrieves its own document and a
different organization retrieves none of it. Two rules in
[`tests/unit/test_architecture.py`](../tests/unit/test_architecture.py) stop the
boundary eroding: every `/platform` endpoint must take the operator dependency,
and only `api/v1/platform.py` may mention `decode_platform_token` or
`PlatformPrincipal`.
