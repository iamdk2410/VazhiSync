/** Line icons from the designs (24 px grid, currentColor). */
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number; strokeWidth?: number };

function Icon({ size = 20, strokeWidth = 2, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const MailIcon = (p: IconProps) => (
  <Icon {...p}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></Icon>
);
export const LockIcon = (p: IconProps) => (
  <Icon {...p}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></Icon>
);
export const PersonIcon = (p: IconProps) => (
  <Icon {...p}><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7" /></Icon>
);
export const EyeIcon = (p: IconProps) => (
  <Icon size={22} {...p}><path d="M2.5 12s3.5-7 9.5-7 9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7z" /><circle cx="12" cy="12" r="3" /></Icon>
);
export const EyeOffIcon = (p: IconProps) => (
  <Icon size={22} {...p}>
    <path d="M3 3l18 18" />
    <path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" />
    <path d="M9.9 5.1A9.8 9.8 0 0 1 12 5c6 0 9.5 7 9.5 7a17 17 0 0 1-3.2 4.1" />
    <path d="M6.1 6.1C3.8 7.7 2.5 12 2.5 12s3.5 7 9.5 7a9.6 9.6 0 0 0 5.9-2.1" />
  </Icon>
);
export const CheckIcon = (p: IconProps) => (
  <Icon size={16} strokeWidth={2.5} {...p}><path d="M5 12.5l4.5 4.5L19 7.5" /></Icon>
);
export const AlertIcon = (p: IconProps) => (
  <Icon size={16} strokeWidth={2.5} {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5" /><path d="M12 16.5h.01" /></Icon>
);
export const BackIcon = (p: IconProps) => (
  <Icon size={24} {...p}><path d="M15 5l-7 7 7 7" /></Icon>
);
export const SignOutIcon = (p: IconProps) => (
  <Icon {...p}><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3" /><path d="M10 17l5-5-5-5" /><path d="M15 12H4" /></Icon>
);
