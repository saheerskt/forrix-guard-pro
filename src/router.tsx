import { createRouter as createTanStackRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";

export function getRouter() {
  return createTanStackRouter({
    routeTree,
    defaultPreload: "intent",
    defaultErrorComponent: ({ error }) => (
      <div className="p-8 text-[--color-bad]">
        <h1 className="text-lg font-semibold">Something went wrong</h1>
        <p className="mt-2 text-sm opacity-80">{error.message}</p>
      </div>
    ),
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>;
  }
}
