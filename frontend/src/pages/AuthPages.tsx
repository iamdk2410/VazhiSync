/** Sign in and create account (Login/Signup and their Mobile* designs). */
import { useState } from "react";
import { Navigate } from "react-router-dom";

import { AuthLayout } from "../components/layout/AuthLayout";
import { LoginForm } from "../features/auth/LoginForm";
import { ROLE_HOME, useSession } from "../features/auth/session";
import { SignupForm, SignupSuccess } from "../features/auth/SignupForm";

const LOGIN_HERO = {
  eyebrow: "For passengers, drivers and officers",
  title: "Keep every bus in step.",
  lead: "Live headway advice that spots buses bunching up and suggests how to space them out again.",
};

const SIGNUP_HERO = {
  eyebrow: "Passenger sign-up",
  title: "Your ride, in sync.",
  lead: "Create a passenger account to follow your routes on one live map.",
};

/** Already signed in? Go home instead of showing the form. */
function useSignedInRedirect() {
  const { status, user } = useSession();
  return status === "signedIn" && user ? <Navigate to={ROLE_HOME[user.role]} replace /> : null;
}

export function LoginPage() {
  const redirect = useSignedInRedirect();
  if (redirect) return redirect;
  return (
    <AuthLayout hero={LOGIN_HERO} kind="login">
      {(variant) => <LoginForm variant={variant} />}
    </AuthLayout>
  );
}

export function SignupPage() {
  const redirect = useSignedInRedirect();
  const [created, setCreated] = useState<string | null>(null);
  if (redirect) return redirect;
  return (
    <AuthLayout hero={SIGNUP_HERO} kind="signup">
      {(variant) =>
        created ? (
          <SignupSuccess variant={variant} email={created} />
        ) : (
          <SignupForm variant={variant} onCreated={setCreated} />
        )
      }
    </AuthLayout>
  );
}
