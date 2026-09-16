import LoginForm from "@/components/LoginForm";

export default function OwnerLogin() {
  return (
    <LoginForm
      console_="owner"
      title="Platform sign in"
      hint="For whoever runs this deployment. Creates organizations and their administrators."
      otherHref="/admin/login"
      otherLabel="Sign in to an organization instead"
    />
  );
}
