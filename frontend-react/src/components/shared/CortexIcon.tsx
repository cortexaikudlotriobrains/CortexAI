import type { SVGProps } from "react";

export type CortexIconName =
  | "analyze"
  | "alert"
  | "ask"
  | "attach"
  | "branch"
  | "check"
  | "chevron-down"
  | "chevron-left"
  | "chevron-right"
  | "collapse-sidebar"
  | "compare"
  | "copy"
  | "cost"
  | "debug"
  | "download"
  | "expand-sidebar"
  | "external-link"
  | "find-solution"
  | "history"
  | "improve"
  | "latency"
  | "moon"
  | "models"
  | "new-chat"
  | "plus"
  | "regenerate"
  | "reply-return"
  | "review"
  | "rewrite"
  | "scroll-down"
  | "search"
  | "send"
  | "smart"
  | "sparkle"
  | "settings"
  | "sources"
  | "stop"
  | "summarize"
  | "sun"
  | "swap"
  | "thumb-down"
  | "thumb-up"
  | "tokens"
  | "tools"
  | "table"
  | "trash"
  | "usage"
  | "user"
  | "web"
  | "work";

interface CortexIconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: CortexIconName;
  size?: number | string;
}

export function CortexIcon({
  name,
  size = 20,
  strokeWidth = 1.75,
  ...props
}: CortexIconProps) {
  if (name === "stop") {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="currentColor"
        aria-hidden="true"
        focusable="false"
        {...props}
      >
        <rect x="7" y="7" width="10" height="10" rx="2" />
      </svg>
    );
  }

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
      {...props}
    >
      {iconPath(name)}
    </svg>
  );
}

function iconPath(name: Exclude<CortexIconName, "stop">) {
  switch (name) {
    case "work":
      return (
        <>
          <rect x="4" y="4.5" width="16" height="15" rx="3" />
          <path d="m7.8 10.4 1.7 1.7 3.3-3.4" />
          <path d="M7.5 15.5h9" />
        </>
      );
    case "tools":
      return (
        <>
          <path d="M9 3.5v3.8M15 3.5v3.8" />
          <path d="M6.8 7.3h10.4v2.9a5.2 5.2 0 0 1-10.4 0z" />
          <path d="M12 15.4v5.1" />
        </>
      );
    case "alert":
      return (
        <>
          <path d="M12 4.6 20.8 20H3.2z" />
          <path d="M12 10.2v3.6M12 16.8h.01" />
        </>
      );
    case "table":
      return (
        <>
          <rect x="4" y="4.5" width="16" height="15" rx="2.5" />
          <path d="M4 9.6h16M9.6 9.6v9.9M14.6 9.6v9.9" />
        </>
      );
    case "settings":
      return (
        <>
          <path d="M4 8.5h9.5M17.5 8.5H20M4 15.5h3.5M11.5 15.5H20" />
          <circle cx="15.5" cy="8.5" r="2.2" />
          <circle cx="9.5" cy="15.5" r="2.2" />
        </>
      );
    case "ask":
      return (
        <>
          <path d="M21 11.5a8.4 8.4 0 0 1-12.3 7.5L4 20.5l1.4-4.5A8.4 8.4 0 1 1 21 11.5z" />
          <path d="M9 10.5h6M9 13.5h4" />
        </>
      );
    case "compare":
      return (
        <>
          <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
          <line x1="12" y1="5" x2="12" y2="19" />
        </>
      );
    case "new-chat":
      return (
        <>
          <path d="M12 20h8" />
          <path d="M16.5 4.5a2.12 2.12 0 0 1 3 3L8 19l-4 1 1-4z" />
        </>
      );
    case "history":
      return (
        <>
          <path d="M3.5 9a8.5 8.5 0 1 1 .3 5" />
          <path d="M3.5 4v5h5" />
          <path d="M12 8v4l3 2" />
        </>
      );
    case "usage":
      return (
        <>
          <path d="M3 21h18" />
          <rect x="5" y="11" width="3.4" height="7" rx="1" />
          <rect x="10.3" y="6" width="3.4" height="12" rx="1" />
          <rect x="15.6" y="13.5" width="3.4" height="4.5" rx="1" />
        </>
      );
    case "models":
      return (
        <>
          <rect x="5" y="5" width="14" height="14" rx="3" />
          <rect x="9.2" y="9.2" width="5.6" height="5.6" rx="1.2" />
          <path d="M9 2.5V5M15 2.5V5M9 19v2.5M15 19v2.5M2.5 9H5M2.5 15H5M19 9h2.5M19 15h2.5" />
        </>
      );
    case "search":
      return (
        <>
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3" />
        </>
      );
    case "collapse-sidebar":
      return (
        <>
          <rect x="3" y="4" width="18" height="16" rx="2.5" />
          <line x1="9" y1="4" x2="9" y2="20" />
          <path d="M15.5 10l-2 2 2 2" />
        </>
      );
    case "expand-sidebar":
      return (
        <>
          <rect x="3" y="4" width="18" height="16" rx="2.5" />
          <line x1="9" y1="4" x2="9" y2="20" />
          <path d="M13.5 10l2 2-2 2" />
        </>
      );
    case "user":
      return (
        <>
          <circle cx="12" cy="8" r="3.4" />
          <path d="M5 20a7 7 0 0 1 14 0" />
        </>
      );
    case "moon":
      return <path d="M20 15.6A8.5 8.5 0 0 1 8.4 4a7 7 0 1 0 11.6 11.6z" />;
    case "sun":
      return (
        <>
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2.8v2M12 19.2v2M4.8 4.8l1.4 1.4M17.8 17.8l1.4 1.4M2.8 12h2M19.2 12h2M4.8 19.2l1.4-1.4M17.8 6.2l1.4-1.4" />
        </>
      );
    case "latency":
      return <path d="M13 3 5 13h5l-1 8 8-11h-5z" />;
    case "tokens":
      return (
        <>
          <path d="M12 3 3 7.5l9 4.5 9-4.5z" />
          <path d="M3 12.5l9 4.5 9-4.5M3 16.5l9 4.5 9-4.5" />
        </>
      );
    case "trash":
      return (
        <>
          <path d="M4 7h16" />
          <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7" />
          <path d="M6.5 7h11l-.7 12A2 2 0 0 1 14.8 21H9.2a2 2 0 0 1-2-1.9z" />
          <path d="M10 11v6M14 11v6" />
        </>
      );
    case "cost":
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M14.6 9.4a2.6 2.6 0 0 0-2.6-1.5c-1.5 0-2.6.8-2.6 2 0 2.7 5.4 1.4 5.4 4.1 0 1.3-1.2 2.1-2.8 2.1a2.7 2.7 0 0 1-2.7-1.6M12 6.4v11.2" />
        </>
      );
    case "attach":
      return <path d="M20 11.5 12 19.5a4.5 4.5 0 0 1-6.4-6.4l8-8a3 3 0 0 1 4.3 4.3l-8 8a1.5 1.5 0 0 1-2.2-2.1l7.3-7.3" />;
    case "smart":
      return (
        <>
          <path d="M12 3c.4 3.6 1.4 4.6 5 5-3.6.4-4.6 1.4-5 5-.4-3.6-1.4-4.6-5-5 3.6-.4 4.6-1.4 5-5z" />
          <path d="M18.5 14.5c.2 1.6.6 2 2.2 2.2-1.6.2-2 .6-2.2 2.2-.2-1.6-.6-2-2.2-2.2 1.6-.2 2-.6 2.2-2.2z" />
        </>
      );
    case "sparkle":
      return <path d="M12 3c.4 3.6 1.4 4.6 5 5-3.6.4-4.6 1.4-5 5-.4-3.6-1.4-4.6-5-5 3.6-.4 4.6-1.4 5-5z" />;
    case "web":
      return (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M3 12h18M12 3c2.5 2.6 2.5 15.4 0 18M12 3c-2.5 2.6-2.5 15.4 0 18" />
        </>
      );
    case "sources":
      return <path d="M9 12a3 3 0 0 0 3 3h2a4 4 0 0 0 0-8h-1M15 12a3 3 0 0 0-3-3h-2a4 4 0 0 0 0 8h1" />;
    case "improve":
      return (
        <>
          <path d="M5 19 14 10" />
          <path d="M16 3l1 2.4 2.4 1-2.4 1L16 9.8 15 7.4 12.6 6.4 15 5.4z" />
          <path d="M6.5 5.5 7 7l1.5.5L7 8l-.5 1.5L6 8l-1.5-.5L6 7z" />
        </>
      );
    case "send":
      return <path d="M5 12h13M12 6l6 6-6 6" />;
    case "copy":
      return (
        <>
          <rect x="9" y="9" width="11" height="11" rx="2.5" />
          <path d="M5 15V6a2 2 0 0 1 2-2h9" />
        </>
      );
    case "regenerate":
      return (
        <>
          <path d="M3.5 9a8.5 8.5 0 1 1 .3 5" />
          <path d="M3.5 4v5h5" />
        </>
      );
    case "reply-return":
      return (
        <>
          <path d="M4 5v6a2 2 0 0 0 2 2h13" />
          <path d="M15 9l4 4-4 4" />
        </>
      );
    case "branch":
      return (
        <>
          <circle cx="6" cy="6" r="2.5" />
          <circle cx="6" cy="18" r="2.5" />
          <circle cx="18" cy="9" r="2.5" />
          <path d="M6 8.5v7M8.5 6H14a2 2 0 0 1 2 2v.5" />
        </>
      );
    case "thumb-up":
      return <path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zm0 0 4.5-8a2 2 0 0 1 2 1.6l-.8 4.4H19a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 17.8 20H7" />;
    case "thumb-down":
      return <path d="M17 13V4h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1zm0 0-4.5 8a2 2 0 0 1-2-1.6l.8-4.4H5a2 2 0 0 1-2-2.3l1.2-6A2 2 0 0 1 6.2 4H17" />;
    case "debug":
      return (
        <>
          <rect x="8" y="8" width="8" height="9" rx="4" />
          <path d="M9 8a3 3 0 0 1 6 0M4 11h3M4 15h3M17 11h3M17 15h3M5.5 7l2 1.8M18.5 7l-2 1.8M12 12v5" />
        </>
      );
    case "summarize":
      return (
        <>
          <rect x="5" y="3" width="14" height="18" rx="2.5" />
          <path d="M8.5 8h7M8.5 12h7M8.5 16h4" />
        </>
      );
    case "rewrite":
      return (
        <>
          <path d="M4 20h4L19 9a2.12 2.12 0 0 0-3-3L5 17z" />
          <path d="M14 7l3 3" />
        </>
      );
    case "analyze":
      return (
        <>
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3M11 8v6M8 11h6" />
        </>
      );
    case "find-solution":
      return (
        <>
          <circle cx="12" cy="12" r="8" />
          <circle cx="12" cy="12" r="3.4" />
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
        </>
      );
    case "review":
      return (
        <>
          <path d="M21 11.5a8.4 8.4 0 0 1-12.3 7.5L4 20.5l1.4-4.5A8.4 8.4 0 1 1 21 11.5z" />
          <path d="M8.5 11.5l2 2 4.5-4.5" />
        </>
      );
    case "plus":
      return <path d="M12 5v14M5 12h14" />;
    case "chevron-down":
      return <path d="M6 9l6 6 6-6" />;
    case "chevron-left":
      return <path d="M15 18l-6-6 6-6" />;
    case "chevron-right":
      return <path d="M9 6l6 6-6 6" />;
    case "download":
      return <path d="M12 3v12M8 11l4 4 4-4M5 21h14" />;
    case "swap":
      return <path d="M8 4 4 8l4 4M4 8h11M16 20l4-4-4-4M20 16H9" />;
    case "scroll-down":
      return <path d="M12 5v14M6 13l6 6 6-6" />;
    case "external-link":
      return <path d="M7 17 17 7M9 7h8v8" />;
    case "check":
      return <path d="m6 12 4 4 8-8" />;
  }
}
