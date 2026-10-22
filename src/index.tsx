import { definePlugin, routerHook } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { FaVolumeUp } from "react-icons/fa";
import { startAppWatcher, stopAppWatcher } from "./appWatcher";
import { DiagnosticsPage } from "./components/DiagnosticsPage";
import { DolbyControlsPage } from "./components/DolbyControlsPage";
import { QuickAccess } from "./components/QuickAccess";
import { SetupPage } from "./components/SetupPage";
import { t } from "./strings";

export default definePlugin(() => {
  routerHook.addRoute("/ally-dsp/setup", SetupPage, { exact: true });
  routerHook.addRoute("/ally-dsp/diagnostics", DiagnosticsPage, { exact: true });
  routerHook.addRoute("/ally-dsp/dolby", DolbyControlsPage, { exact: true });
  startAppWatcher();
  return {
    name: t.title,
    titleView: <div className={staticClasses.Title}>{t.title}</div>,
    content: <QuickAccess />,
    icon: <FaVolumeUp />,
    onDismount() {
      stopAppWatcher();
      routerHook.removeRoute("/ally-dsp/setup");
      routerHook.removeRoute("/ally-dsp/diagnostics");
      routerHook.removeRoute("/ally-dsp/dolby");
    },
  };
});
