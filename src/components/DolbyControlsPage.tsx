import {
  ButtonItem, DialogButton, Field, Focusable, Navigation, PanelSection,
  PanelSectionRow, SliderField, ToggleField,
} from "@decky/ui";
import { addEventListener, removeEventListener, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { getOriginalControls, prepareOriginalProfiles, setOriginalControls } from "../backend";
import { DebouncedValueWriter } from "../hooks/debouncedValueWriter";
import { t } from "../strings";
import type { OriginalControls, OriginalControlsState } from "../types";
import { Page, ScrollArea } from "./Page";

function frequencyLabel(value: number): string {
  return value >= 1000 ? `${Number((value / 1000).toFixed(2))} kHz` : `${value} Hz`;
}

export function DolbyControlsPage() {
  const [state, setState] = useState<OriginalControlsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<OriginalControls | null>(null);
  const [preparing, setPreparing] = useState(false);
  const stateRef = useRef<OriginalControlsState | null>(null);
  const draft = useRef<OriginalControls>({});
  const pendingRef = useRef<OriginalControls | null>(null);
  const writer = useRef<DebouncedValueWriter<OriginalControls, OriginalControlsState> | null>(null);

  useEffect(() => {
    let mounted = true;
    const accept = (next: OriginalControlsState) => {
      stateRef.current = next;
      if (mounted) setState(next);
    };
    const controlWriter = new DebouncedValueWriter<OriginalControls, OriginalControlsState>({
      delay: 250,
      write: async (overrides) => {
        const result = await setOriginalControls(overrides);
        if (!result.ok || !result.state.available) throw new Error(t.dolbyControlsFailed);
        return result.state;
      },
      onCommit: (next, latest) => {
        accept(next);
        if (latest) {
          draft.current = next.overrides;
          pendingRef.current = null;
          setPending(null);
          setError(null);
        }
      },
      onError: (saveError, latest) => {
        if (latest) {
          draft.current = stateRef.current?.overrides ?? {};
          pendingRef.current = null;
          setPending(null);
        }
        toaster.toast({ title: t.title, body: String(saveError) });
      },
    });
    writer.current = controlWriter;
    const onControls = (next: OriginalControlsState) => {
      if (pendingRef.current === null) draft.current = next.overrides ?? {};
      accept(next);
    };
    addEventListener<[OriginalControlsState]>("original_controls_state", onControls);
    void getOriginalControls().then((next) => {
      if (!mounted) return;
      draft.current = next.overrides ?? {};
      accept(next);
    }).catch((loadError: unknown) => {
      if (mounted) setError(String(loadError));
    });
    return () => {
      mounted = false;
      controlWriter.dispose();
      removeEventListener("original_controls_state", onControls);
      if (writer.current === controlWriter) writer.current = null;
    };
  }, []);

  const submit = (overrides: OriginalControls) => {
    if (!state?.available || preparing) return;
    draft.current = overrides;
    pendingRef.current = overrides;
    setPending(overrides);
    writer.current?.submit(overrides);
  };
  const change = (values: OriginalControls) => submit({ ...draft.current, ...values });
  const prepare = async () => {
    if (preparing) return;
    setPreparing(true);
    try {
      const result = await prepareOriginalProfiles();
      if (!result.ok) throw new Error(result.state.error || t.dolbyUnavailable);
      stateRef.current = result.state;
      draft.current = result.state.overrides ?? {};
      setState(result.state);
      setError(null);
    } catch (prepareError) {
      setError(String(prepareError));
    } finally {
      setPreparing(false);
    }
  };
  const effective: OriginalControls = state
    ? { ...(state.official ?? state.effective), ...(pending ?? state.overrides) } : {};
  const bands = effective.eqBandsDb ?? Array<number>(20).fill(0);
  const context = state?.available
    ? `${state.route ?? ""} · ${state.preset ?? ""} · ${state.voicing ?? ""}` : t.dolbyUnavailable;

  return (
    <Page>
      <h2 style={{ marginBottom: "6px" }}>{t.dolbyControls}</h2>
      <Focusable style={{ display: "flex", gap: "12px", marginBottom: "8px" }} flow-children="horizontal">
        <DialogButton onClick={() => Navigation.NavigateBack()}>{t.back}</DialogButton>
      </Focusable>
      <p aria-live="polite" style={{ margin: "0 0 10px", fontSize: "13px", opacity: 0.8 }}>
        {context}{state?.available ? ` · ${pending !== null ? t.dolbySaving : t.dolbySaved}` : ""}
      </p>
      <ScrollArea>
        {(error || state?.error) && <PanelSection>
          <PanelSectionRow><Field label={t.dolbyUnavailable} description={error || state?.error || undefined} /></PanelSectionRow>
        </PanelSection>}
        {!state && !error && <p>…</p>}
        {state && !state.available && <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={preparing} description={t.dolbyPrepareDesc} onClick={() => void prepare()}>
              {preparing ? t.dolbyPreparing : t.dolbyPrepare}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>}
        {state?.available && <>
          <PanelSection title={t.sound}>
            <PanelSectionRow>
              <ToggleField label={t.dolbySpatial} description={t.dolbySpatialDesc} checked={effective.spatialEnabled ?? true}
                onChange={(value) => change({ spatialEnabled: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label={t.dolbyIeq} description={t.dolbyIeqDesc} checked={effective.ieqEnabled ?? false}
                onChange={(value) => change({ ieqEnabled: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField label={t.dolbyIeqAmount} value={effective.ieqAmount ?? 0} min={0} max={16} step={1} showValue
                onChange={(value) => change({ ieqAmount: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label={t.dialog} checked={effective.dialogEnabled ?? false}
                onChange={(value) => change({ dialogEnabled: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField label={t.dolbyDialogAmount} value={effective.dialogAmount ?? 0} min={0} max={16} step={1} showValue
                onChange={(value) => change({ dialogAmount: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label={t.autogain} description={t.autogainDesc} checked={effective.volumeLevelerEnabled ?? false}
                onChange={(value) => change({ volumeLevelerEnabled: value })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField label={t.dolbyLevelerAmount} value={effective.volumeLevelerAmount ?? 0} min={0} max={10} step={1} showValue
                onChange={(value) => change({ volumeLevelerAmount: value })} />
            </PanelSectionRow>
          </PanelSection>
          <PanelSection title={t.dolbyEq}>
            <PanelSectionRow>
              <ToggleField label={t.dolbyEq} description={t.dolbyEqDesc} checked={effective.eqEnabled ?? false}
                onChange={(value) => change({ eqEnabled: value })} />
            </PanelSectionRow>
            {state.frequencies.map((frequency, index) => (
              <PanelSectionRow key={index}>
                <SliderField label={frequencyLabel(frequency)} value={bands[index] ?? 0}
                  min={-12} max={12} step={0.5} showValue valueSuffix=" dB"
                  onChange={(value) => {
                    const current = draft.current.eqBandsDb ?? stateRef.current?.official.eqBandsDb ?? bands;
                    const next = [...current];
                    next[index] = Math.max(-12, Math.min(12, Math.round(value * 2) / 2));
                    change({ eqBandsDb: next });
                  }} />
              </PanelSectionRow>
            ))}
            <PanelSectionRow>
              <ButtonItem layout="below" description={t.dolbyFlatDesc} onClick={() => change({ eqBandsDb: Array<number>(20).fill(0) })}>
                {t.dolbyFlat}
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
          <PanelSection title={t.maintenance}>
            <PanelSectionRow>
              <ButtonItem layout="below" description={t.dolbyRestoreDesc} onClick={() => submit({})}>{t.dolbyRestore}</ButtonItem>
            </PanelSectionRow>
          </PanelSection>
        </>}
      </ScrollArea>
    </Page>
  );
}
