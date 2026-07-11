import { AppShell } from "@/components/app-shell";
import { DocumentViewerPage } from "@/routes/document-viewer";
import { LibrariesPage } from "@/routes/libraries";
import { LibraryAskPage } from "@/routes/library-ask";
import { LibraryDetailPage } from "@/routes/library-detail";
import { Outlet, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";

const rootRoute = createRootRoute({
  component: () => (
    <AppShell>
      <Outlet />
    </AppShell>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: LibrariesPage,
});

const libraryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/library/$libraryId",
  component: LibraryDetailPage,
});

const documentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/library/$libraryId/document/$docId",
  component: DocumentViewerPage,
});

const askRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/library/$libraryId/ask",
  component: LibraryAskPage,
});

const routeTree = rootRoute.addChildren([indexRoute, libraryRoute, documentRoute, askRoute]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
