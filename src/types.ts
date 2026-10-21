export interface Codec {
  card: number;
  card_id: string;
  codec: string;
  vendor_id: string;
  dev: string;
  ssid: string;
  supported: boolean;
  model: string | null;
}

export interface Resolved {
  profile: string;
  voicing: string;
  source: "global" | "app";
  appId: string | null;
}

export interface PerAppEntry {
  profile: string;
  voicing: string;
  enabled: boolean;
  name?: string;
}

export interface Extras {
  autogain: boolean;
  dialog: boolean;
  regulator: boolean;
  virtualBass: boolean;
  preGainDb: number;
}

export type EngineKind = "converted" | "original";

export interface EngineState {
  kind: EngineKind;
  /** Availability of the locally imported original engine assets. */
  available: boolean;
  error?: string | null;
  engineLabel?: string | null;
  scope?: string | null;
}

export interface Settings {
  enabled: boolean;
  engine?: { kind: EngineKind };
  global: { profile: string; voicing: string };
  perApp: Record<string, PerAppEntry>;
  extras: Extras;
  update: { channel: string; lastCheck: number; autoCheck: boolean; error?: string | null };
  setup: {
    done: boolean;
    packageVersion?: string | null;
    converterVersion?: string | null;
    completedAt?: string | null;
    targetSink?: string | null;
  };
}

export interface SetupProgress {
  step: string;
  index: number;
  total: number;
  status: "running" | "done" | "skipped" | "error" | "cancelled";
  message: string;
  percent: number;
  package?: any;
}

export interface DspState {
  unit_installed: boolean;
  unit_current: boolean;
  enabled: boolean;
  active: boolean;
  verified: boolean;
  links: { input: number; output: number };
  active_preset: { profile?: string; voicing?: string; preGainDb?: number; applied_at?: string } | null;
  jack: { headphones: boolean | null; paused: boolean };
  enabled_setting: boolean;
  engine?: EngineKind;
  original?: { ready?: boolean; state?: string; error?: string | null };
}

export interface UpdateInfo {
  currentVersion: string;
  latestVersion: string | null;
  updateAvailable: boolean;
  releaseUrl?: string | null;
  error?: string | null;
}

export interface PluginState {
  version: string;
  engine?: EngineState;
  setup: Settings["setup"] & {
    xmlPresent: boolean;
    venvOk: boolean;
    presets: Record<string, Record<string, boolean>>;
    inProgress: boolean;
    last: SetupProgress | null;
    converting: boolean;
    convertLast: { percent: number; message: string; status: string } | null;
  };
  hardware: {
    codec: Codec | null;
    dmi: { product_name: string; sys_vendor: string; bios_version: string };
    kernel: string;
    sink: { name: string; description: string } | null;
    route: { name: string; available: string } | null;
    headphones: boolean;
    lv2: { ok: boolean; missing: string[]; calf?: boolean } | null;
  };
  dsp: DspState;
  settings: Settings;
  runningApp: { appId: string | null; resolved: Resolved };
  update: UpdateInfo;
  profiles: { id: string; label: string }[];
  voicings: { id: string; label: string }[];
}

export interface UpdateArtifact {
  artifact: string;
  name: string;
  version: string;
  hash: string;
  detail: string;
}
