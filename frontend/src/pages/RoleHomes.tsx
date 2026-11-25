/** Simple home pages per role, replaced by the real screens in B14 (officer), B15 (driver)
 *  and B16 (passenger). */
import { RoleHome } from "../components/layout/AppShell";

export function OfficerHome() {
  return (
    <RoleHome
      greeting="Good to see you"
      lead="Bunching alerts and the advisor's recommendations for route 32B will appear here for you to approve, edit or reject."
      cards={[
        { title: "Live route map", text: "Every bus on 32B with its headway, refreshed live." },
        { title: "Recommendations", text: "Hold, skip-stop or short-turn proposals, ranked by the simulator." },
        { title: "Decisions log", text: "What was approved, sent and acknowledged, with the reasons." },
      ]}
    />
  );
}

export function DriverHome() {
  return (
    <RoleHome
      greeting="Welcome aboard"
      lead="Your bus and the instructions approved by the control officer will appear here."
      cards={[
        { title: "My bus", text: "Your position, the bus ahead and the bus behind." },
        { title: "Instructions", text: "Approved instructions to acknowledge, one tap each." },
      ]}
    />
  );
}

export function PassengerHome() {
  return (
    <RoleHome
      greeting="Hello"
      lead="Arrivals, crowding and service alerts for your stops on route 32B will appear here."
      cards={[
        { title: "Arrivals", text: "When the next buses reach your stop." },
        { title: "Crowding", text: "How full each approaching bus is." },
        { title: "Service alerts", text: "Short-turns and skipped stops that affect you." },
      ]}
    />
  );
}
