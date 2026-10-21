import { callable } from "@decky/api";
import type { DspState, EngineKind, EngineState, Extras, PerAppEntry, PluginState, SetupProgress, UpdateArtifact, UpdateInfo } from "./types";

export const getState = callable<[], PluginState>("get_state");
export const runSetup = callable<[force: boolean, allowUnsupported: boolean], { started: boolean; reason?: string }>("run_setup");
export const cancelSetup = callable<[], { cancelling: boolean }>("cancel_setup");
export const getSetupProgress = callable<[], SetupProgress | null>("get_setup_progress");
export const setGlobal = callable<[profile: string, voicing: string], { ok: boolean }>("set_global");
export const setPerApp = callable<[appId: string, entry: PerAppEntry | null], { ok: boolean }>("set_per_app");
export const onRunningAppChanged = callable<[appId: string | null], { appId: string | null }>("on_running_app_changed");
export const setEnabled = callable<[enabled: boolean], DspState>("set_enabled");
export const setEngine = callable<[kind: EngineKind], { ok: boolean; engine: EngineState }>("set_engine");
export const setOriginalGain = callable<[gain_db: number], { ok: boolean; outputGainDb: number; applied: boolean }>("set_original_gain");
export const setExtras = callable<[extras: Partial<Extras>], { ok: boolean; extras: Extras; reconverting: boolean }>("set_extras");
export const checkForUpdate = callable<[force: boolean], UpdateInfo>("check_for_update");
export const prepareUpdate = callable<[], UpdateArtifact>("prepare_update");
export const getDiagnostics = callable<[], { text: string }>("get_diagnostics");

/** Hand the verified release to Decky Loader's installer. */
export async function installViaDecky(a: UpdateArtifact): Promise<void> {
  const backend = window.DeckyBackend;
  if (!backend?.callable) throw new Error("Decky install API not available");
  const install = backend.callable<[string, string, string, string, number], void>("utilities/install_plugin");
  // InstallType.UPDATE = 2 (decky-loader frontend enum)
  await install(a.artifact, a.name, a.version, a.hash, 2);
}
