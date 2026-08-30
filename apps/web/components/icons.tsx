import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="20"
      viewBox="0 0 24 24"
      width="20"
      {...props}
    >
      {children}
    </svg>
  );
}

export function MarkIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 17 10.5 10.5 14 14l6-8" stroke="currentColor" strokeWidth="1.8" />
      <path d="M14.5 6H20v5.5" stroke="currentColor" strokeWidth="1.8" />
    </Icon>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 5.5h16v11H9l-5 3v-14Z" stroke="currentColor" strokeWidth="1.7" />
    </Icon>
  );
}

export function ChartIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 19V5M4 19h16" stroke="currentColor" strokeWidth="1.7" />
      <path d="m7 15 3-4 3 2 5-7" stroke="currentColor" strokeWidth="1.7" />
    </Icon>
  );
}

export function PulseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 12h4l2-6 4 12 2-6h6" stroke="currentColor" strokeWidth="1.7" />
    </Icon>
  );
}

export function GridIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect height="6" rx="1" stroke="currentColor" width="6" x="4" y="4" />
      <rect height="6" rx="1" stroke="currentColor" width="6" x="14" y="4" />
      <rect height="6" rx="1" stroke="currentColor" width="6" x="4" y="14" />
      <rect height="6" rx="1" stroke="currentColor" width="6" x="14" y="14" />
    </Icon>
  );
}

export function TrackingIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="12" fill="currentColor" r="1.6" />
      <path d="m12 12 4-4M12 4v2M20 12h-2" stroke="currentColor" strokeWidth="1.5" />
    </Icon>
  );
}

export function PortfolioIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="6" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="18" cy="7" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="17" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="m8.2 9.2 2.7 5.5m4.8-5.8-2.6 5.7M8.5 7.8l7-.5" stroke="currentColor" strokeWidth="1.4" />
    </Icon>
  );
}

export function FactorIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 6h14M5 12h14M5 18h14" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="9" cy="6" fill="currentColor" r="2" />
      <circle cx="15" cy="12" fill="currentColor" r="2" />
      <circle cx="11" cy="18" fill="currentColor" r="2" />
    </Icon>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a8 8 0 0 0-1.8-1L14.4 3h-4.8l-.3 3.1a8 8 0 0 0-1.8 1l-2.4-1-2 3.4 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.4-1a8 8 0 0 0 1.8 1l.3 3.1h4.8l.3-3.1a8 8 0 0 0 1.8-1l2.4 1 2-3.4-2-1.5a7 7 0 0 0 .1-1Z"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </Icon>
  );
}
