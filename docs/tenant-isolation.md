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
pairs and contains no tenant data.

Both are listed in `app/models/__init__.py::GLOBAL_TABLES`, and an integration
test asserts that **no other table** lacks RLS — so nothing ends up outside
tenant scoping by accident.

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
