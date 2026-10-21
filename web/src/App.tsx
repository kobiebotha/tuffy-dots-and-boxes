import { lazy, Suspense, useEffect, useState } from "react";
import { Install } from "./components/Install";
import { PageFrame } from "./components/PageFrame";

const DashboardApp = lazy(() => import("./DashboardApp"));
const EmulatorApp = lazy(() => import("./Emulator").then((module) => ({ default: module.EmulatorApp })));

function useRoute(blocked: boolean) {
  const [path, setPath] = useState(() => window.location.pathname.replace(/\/$/, "") || "/");
  useEffect(() => {
    const sync = () => {
      if (blocked) window.history.pushState(null, "", path);
      else setPath(window.location.pathname.replace(/\/$/, "") || "/");
    };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [blocked, path]);
  function go(next: string) {
    if (blocked) return;
    window.history.pushState(null, "", next);
    setPath(next);
    window.scrollTo(0, 0);
  }
  return { path, go };
}

export function App() {
  const [installBusy, setInstallBusy] = useState(false);
  const { path, go } = useRoute(installBusy);
  if (path === "/install") {
    return (
      <PageFrame path={path} go={go} busy={installBusy}>
        <Install onBusyChange={setInstallBusy} />
      </PageFrame>
    );
  }
  return (
    <Suspense fallback={<main className="shell boot-screen"><p role="status">Getting the board ready…</p></main>}>
      {path === "/emulate" ? <EmulatorApp /> : <DashboardApp path={path} go={go} />}
    </Suspense>
  );
}
