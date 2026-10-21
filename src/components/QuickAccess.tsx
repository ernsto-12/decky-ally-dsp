import {
  ButtonItem,
  DropdownItem,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { appName } from "../appWatcher";
import {
  checkForUpdate,
  installViaDecky,
  prepareUpdate,
  setEngine,
  setEnabled,
  setExtras,
  setGlobal,
  setOriginalGain,
  setPerApp,
} from "../backend";
import { usePluginState } from "../hooks/usePluginState";
import { DebouncedValueWriter } from "../hooks/debouncedValueWriter";
import { setupIntent } from "../setupIntent";
import { t } from "../strings";
import type { EngineKind, PluginState } from "../types";
import { FRONTEND_VERSION } from "../version";

function goto(path: string) {
  Navigation.Navigate(path);
  Navigation.CloseSideMenus();
}

function statusText(s: PluginState): string {
  if ((s.engine?.kind ?? s.settings.engine?.kind) === "original") {
    if (!s.settings.enabled) return `${t.inactive} (Bypass)`;
    return s.dsp.active && s.dsp.verified && s.dsp.original?.ready ? t.active : t.inactive;
  }
  if (!s.setup.done) return t.notSetUp;
  if (s.dsp.jack?.paused || (s.hardware.headphones && !s.dsp.active)) return t.pausedHeadphones;
  if (!s.settings.enabled) return `${t.inactive} (Bypass)`;
  return s.dsp.active && s.dsp.verified ? t.active : t.inactive;
}

function labelOf(list: { id: string; label: string }[], id: string): string {
  return list.find((x) => x.id === id)?.label ?? id;
}

let reopenTried = false;

/** After an update Decky has loaded the new bundle but keeps showing this old panel; select the plugin again. */
function reopenWithNewUi(): void {
  if (reopenTried) return;
  reopenTried = true;
  try {
    window.DeckyPluginLoader?.deckyState?.setActivePlugin?.(t.title);
  } catch {
    /* the stale-UI row stays as a hint */
  }
}

export function QuickAccess() {
  const { state, error, refresh, setState } = usePluginState();
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<number | null>(null);
  const [pendingOriginalGain, setPendingOriginalGain] = useState<number | null>(null);
  const gainTimer = useRef<number | null>(null);
  const originalGainWriter = useRef<DebouncedValueWriter | null>(null);
  const operationBusy = useRef(false);
  const stale = !!state && state.version !== FRONTEND_VERSION;

  useEffect(() => () => {
    if (gainTimer.current) window.clearTimeout(gainTimer.current);
  }, []);

  useEffect(() => {
    const writer = new DebouncedValueWriter({
      delay: 300,
      write: async (gain) => {
        const result = await setOriginalGain(gain);
        if (!result.ok || !Number.isFinite(result.outputGainDb)) throw new Error(t.originalOutputBoostFailed);
        return result.outputGainDb;
      },
      onCommit: (gain, latest) => {
        setState((current) => current ? {
          ...current,
          settings: { ...current.settings, original: { ...current.settings.original, outputGainDb: gain } },
        } : current);
        if (latest) setPendingOriginalGain(null);
      },
      onError: (saveError, latest) => {
        if (latest) setPendingOriginalGain(null);
        toaster.toast({ title: t.title, body: String(saveError) });
      },
    });
    originalGainWriter.current = writer;
    return () => {
      writer.dispose();
      if (originalGainWriter.current === writer) originalGainWriter.current = null;
    };
  }, [setState]);

  useEffect(() => {
    if (stale) reopenWithNewUi();
  }, [stale]);

  if (error) {
    return (
      <PanelSection title={t.status}>
        <PanelSectionRow>
          <Field label="Backend" description={error} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void refresh()}>{t.refresh}</ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  if (!state) {
    return (
      <PanelSection title={t.status}>
        <PanelSectionRow>
          <Field label="…" />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const s = state;
  const engineKind = s.engine?.kind ?? s.settings.engine?.kind ?? "converted";
  const original = engineKind === "original";
  const originalAvailable = s.engine?.available === true;
  const originalLabel = s.engine?.engineLabel || t.engineOriginal;
  const engineOptions = [
    { data: "converted" as EngineKind, label: t.engineConverted },
    ...(originalAvailable || original ? [{ data: "original" as EngineKind, label: originalLabel }] : []),
  ];
  const engineBusy = busy || s.setup.inProgress || s.setup.converting;
  const workerError = s.dsp.original?.error || (!originalAvailable ? s.engine?.error : null);
  const workerReady = s.dsp.active && s.dsp.verified && s.dsp.original?.ready;
  const resolved = s.runningApp.resolved;
  const appId = s.runningApp.appId;
  const perApp = appId ? s.settings.perApp[appId] : undefined;
  const profileOptions = s.profiles.map((p) => ({ data: p.id, label: p.label }));
  const voicingOptions = s.voicings.map((v) => ({ data: v.id, label: v.label }));
  const activeLabel = original ? originalLabel : s.dsp.active_preset?.profile
    ? `${labelOf(s.profiles, s.dsp.active_preset.profile)} · ${labelOf(s.voicings, s.dsp.active_preset.voicing ?? "")}`
    : "–";

  const run = async (fn: () => Promise<unknown>) => {
    if (operationBusy.current) return;
    operationBusy.current = true;
    setBusy(true);
    const gainWriter = originalGainWriter.current;
    gainWriter?.pause();
    try {
      await gainWriter?.waitForActive();
      await fn();
    } catch (e) {
      toaster.toast({ title: t.title, body: String(e) });
    } finally {
      await refresh();
      operationBusy.current = false;
      setBusy(false);
      gainWriter?.resume();
    }
  };

  const onEngine = (kind: EngineKind) => {
    if (kind === engineKind || engineBusy || operationBusy.current) return;
    if (kind === "original" && !originalAvailable) {
      toaster.toast({ title: t.title, body: s.engine?.error || t.engineUnavailableDesc });
      return;
    }
    // Do not send a delayed converter control after selecting another engine.
    if (gainTimer.current) {
      window.clearTimeout(gainTimer.current);
      gainTimer.current = null;
    }
    originalGainWriter.current?.cancel();
    setPending(null);
    setPendingOriginalGain(null);
    void run(async () => {
      const result = await setEngine(kind);
      if (!result.ok) throw new Error(t.workerError);
    });
  };

  const onGain = (value: number) => {
    setPending(value);
    if (gainTimer.current) window.clearTimeout(gainTimer.current);
    gainTimer.current = window.setTimeout(() => {
      void run(() => setExtras({ preGainDb: value }));
      setPending(null);
    }, 700);
  };

  const onOriginalGain = (value: number) => {
    if (!original || engineBusy || operationBusy.current || !Number.isFinite(value)) return;
    const gain = Math.max(0, Math.min(12, Math.round(value * 2) / 2));
    setPendingOriginalGain(gain);
    // Keep the focused slider enabled while saving. Disabling a Steam slider
    // transfers controller focus, so the next Left/Right can leave this menu.
    originalGainWriter.current?.submit(gain);
  };

  const onInstallUpdate = () =>
    run(async () => {
      const artifact = await prepareUpdate();
      await installViaDecky(artifact);
    });

  return (
    <>
      <PanelSection title={t.status}>
        <PanelSectionRow>
          <Field label={statusText(s)} description={`${activeLabel}${!original && resolved.source === "app" ? ` · ${t.thisGame}` : ""}`} />
        </PanelSectionRow>
        {stale && (
          <PanelSectionRow>
            <Field label={t.staleUi} description={`UI ${FRONTEND_VERSION}, backend ${s.version}`} />
          </PanelSectionRow>
        )}
        {!s.setup.done && !original && (
          <PanelSectionRow>
            <ButtonItem layout="below" description={t.setupNeeded} onClick={() => { setupIntent.force = false; goto("/ally-dsp/setup"); }}>
              {t.runSetup}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {s.setup.inProgress && s.setup.last && (
          <PanelSectionRow>
            <ProgressBarWithInfo nProgress={s.setup.last.percent} sOperationText={s.setup.last.message} layout="below" bottomSeparator="none" />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title={t.engine}>
        <PanelSectionRow>
          <DropdownItem label={t.engine} rgOptions={engineOptions} selectedOption={engineKind} disabled={engineBusy}
            onChange={(option) => onEngine(option.data)} />
        </PanelSectionRow>
        {!originalAvailable && (
          <PanelSectionRow>
            <Field label={t.engineUnavailable} description={s.engine?.error || t.engineUnavailableDesc} />
          </PanelSectionRow>
        )}
        {original && (
          <>
            <PanelSectionRow>
              <Field label={originalLabel} description={t.engineFixedProfile} />
            </PanelSectionRow>
            {s.engine?.scope && (
              <PanelSectionRow>
                <Field label={t.engineScope} description={s.engine.scope} />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <Field label={t.worker} description={!s.settings.enabled ? t.workerStopped : workerError
                ? `${t.workerError}: ${workerError}` : workerReady ? t.workerReady : t.workerStarting} />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      {(s.setup.done || original) && (
        <>
          <PanelSection title={t.sound}>
            <PanelSectionRow>
              <ToggleField label={t.dspEnabled} description={t.dspEnabledDesc} checked={s.settings.enabled} disabled={busy}
                onChange={(v) => void run(() => setEnabled(v))} />
            </PanelSectionRow>
            {original && (
              <PanelSectionRow>
                <SliderField label={t.originalOutputBoost} description={t.originalOutputBoostDesc}
                  value={pendingOriginalGain ?? s.settings.original?.outputGainDb ?? 0}
                  min={0} max={12} step={0.5} showValue valueSuffix=" dB" disabled={engineBusy}
                  onChange={onOriginalGain} />
              </PanelSectionRow>
            )}
            {!original && <>
            <PanelSectionRow>
              <DropdownItem label={t.preset} rgOptions={profileOptions} selectedOption={s.settings.global.profile} disabled={busy}
                onChange={(o) => void run(() => setGlobal(o.data, s.settings.global.voicing))} />
            </PanelSectionRow>
            <PanelSectionRow>
              <DropdownItem label={t.voicing} rgOptions={voicingOptions} selectedOption={s.settings.global.voicing} disabled={busy}
                onChange={(o) => void run(() => setGlobal(s.settings.global.profile, o.data))} />
            </PanelSectionRow>
            </>}
          </PanelSection>

          {!original && <>
          <PanelSection title={appId ? `${t.thisGame}: ${appName(appId)}` : t.thisGame}>
            {appId ? (
              <>
                <PanelSectionRow>
                  <ToggleField label={t.perGameToggle} checked={!!perApp} disabled={busy}
                    onChange={(v) => void run(() => setPerApp(appId, v
                      ? { profile: resolved.profile, voicing: resolved.voicing, enabled: true, name: appName(appId) }
                      : null))} />
                </PanelSectionRow>
                {perApp && (
                  <>
                    <PanelSectionRow>
                      <DropdownItem label={t.perGamePreset} rgOptions={profileOptions} selectedOption={perApp.profile} disabled={busy}
                        onChange={(o) => void run(() => setPerApp(appId, { ...perApp, profile: o.data }))} />
                    </PanelSectionRow>
                    <PanelSectionRow>
                      <DropdownItem label={t.voicing} rgOptions={voicingOptions} selectedOption={perApp.voicing} disabled={busy}
                        onChange={(o) => void run(() => setPerApp(appId, { ...perApp, voicing: o.data }))} />
                    </PanelSectionRow>
                  </>
                )}
              </>
            ) : (
              <>
                <PanelSectionRow>
                  <Field description={t.noGame} />
                </PanelSectionRow>
                {Object.entries(s.settings.perApp).slice(0, 8).map(([id, entry]) => (
                  <PanelSectionRow key={id}>
                    <ButtonItem layout="below" label={`${entry.name || id}: ${labelOf(s.profiles, entry.profile)} · ${labelOf(s.voicings, entry.voicing)}`}
                      disabled={busy} onClick={() => void run(() => setPerApp(id, null))}>
                      {t.remove}
                    </ButtonItem>
                  </PanelSectionRow>
                ))}
              </>
            )}
          </PanelSection>

          <PanelSection title={t.extras}>
            {s.setup.converting && s.setup.convertLast && (
              <PanelSectionRow>
                <ProgressBarWithInfo nProgress={s.setup.convertLast.percent} sOperationText={`${t.reconverting} ${s.setup.convertLast.message}`} layout="below" bottomSeparator="none" />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <ToggleField label={t.autogain} description={t.autogainDesc} checked={s.settings.extras.autogain} disabled={busy || s.setup.converting || s.setup.inProgress}
                onChange={(v) => void run(() => setExtras({ autogain: v }))} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label={t.dialog} checked={s.settings.extras.dialog} disabled={busy || s.setup.converting || s.setup.inProgress}
                onChange={(v) => void run(() => setExtras({ dialog: v }))} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label={t.regulator} description={t.regulatorDesc} checked={s.settings.extras.regulator} disabled={busy || s.setup.converting || s.setup.inProgress}
                onChange={(v) => void run(() => setExtras({ regulator: v }))} />
            </PanelSectionRow>
            {s.hardware.lv2?.calf && (
              <PanelSectionRow>
                <ToggleField label={t.virtualBass} checked={s.settings.extras.virtualBass} disabled={busy || s.setup.converting || s.setup.inProgress}
                  onChange={(v) => void run(() => setExtras({ virtualBass: v }))} />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <SliderField label={t.preGain} value={pending ?? s.settings.extras.preGainDb} min={-6} max={6} step={1} showValue valueSuffix=" dB"
                notchCount={13} disabled={busy || s.setup.inProgress} onChange={onGain} />
            </PanelSectionRow>
          </PanelSection>
          </>}
        </>
      )}

      <PanelSection title={t.maintenance}>
        {s.update.updateAvailable ? (
          <PanelSectionRow>
            <ButtonItem layout="below" description={`${t.updateAvailable}: v${s.update.latestVersion} (${t.version} ${s.version})`} disabled={busy}
              onClick={() => void onInstallUpdate()}>
              {t.installUpdate}
            </ButtonItem>
          </PanelSectionRow>
        ) : (
          <PanelSectionRow>
            <ButtonItem layout="below" description={s.update.error ? `${t.updateFailed}: ${s.update.error}` : `${t.upToDate} · ${t.version} ${s.version}`} disabled={busy}
              onClick={() => void run(() => checkForUpdate(true))}>
              {t.checkUpdate}
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => goto("/ally-dsp/diagnostics")}>{t.diagnostics}</ButtonItem>
        </PanelSectionRow>
        {s.setup.done && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy || s.setup.inProgress} onClick={() => { setupIntent.force = true; goto("/ally-dsp/setup"); }}>
              {t.rerunSetup}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}
