import { createRootRoute, Outlet, HeadContent, Scripts } from "@tanstack/react-router";
import type { ReactNode } from "react";
import "../styles.css";

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "MDGuard — Maximum Demand Intelligence" },
      {
        name: "description",
        content:
          "MDGuard by ForrixGuard — industrial HMI for maximum-demand monitoring, BESS dispatch advisory, and tariff ROI reporting.",
      },
    ],
    links: [
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap",
      },
      {
        rel: "stylesheet",
        href: "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css",
      },
    ],
  }),
  shellComponent: RootDocument,
  notFoundComponent: () => (
    <div className="p-8">
      <h1 className="text-lg font-semibold">404</h1>
      <p className="opacity-70">Page not found.</p>
    </div>
  ),
});

function RootDocument({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <HeadContent />
      </head>
      <body>
        <Outlet />
        {children}
        <Scripts />
      </body>
    </html>
  );
}
