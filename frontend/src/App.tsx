import { Route, Routes } from "react-router-dom";

import { RequireRole } from "./features/auth/RequireRole";
import { LoginPage, SignupPage } from "./pages/AuthPages";
import { NotFound } from "./pages/NotFound";
import { DriverHome, OfficerHome, PassengerHome } from "./pages/RoleHomes";
import { SplashPage } from "./pages/SplashPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<SplashPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/officer" element={<RequireRole role="officer"><OfficerHome /></RequireRole>} />
      <Route path="/driver" element={<RequireRole role="driver"><DriverHome /></RequireRole>} />
      <Route path="/passenger" element={<RequireRole role="passenger"><PassengerHome /></RequireRole>} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
