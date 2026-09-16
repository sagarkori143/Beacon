import LoginForm from "@/components/LoginForm";

export default function AdminLogin() {
  return (
    <LoginForm
      console_="admin"
      title="Organization sign in"
      hint="For an organization's administrator. Manage your branches, people and knowledge."
      otherHref="/owner/login"
      otherLabel="Platform operator sign in"
    />
  );
}
