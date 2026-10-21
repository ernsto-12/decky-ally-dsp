interface Scheduler {
  now(): number;
  setTimeout(callback: () => void, delay: number): ReturnType<typeof setTimeout>;
  clearTimeout(timer: ReturnType<typeof setTimeout>): void;
}

interface Options {
  delay: number;
  write(value: number): Promise<number>;
  onCommit(value: number, latest: boolean): void;
  onError(error: unknown, latest: boolean): void;
  scheduler?: Scheduler;
}

/** Coalesce slider input while keeping at most one backend write in flight. */
export class DebouncedValueWriter {
  private readonly scheduler: Scheduler;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: { value: number; revision: number; due: number } | null = null;
  private active: Promise<void> | null = null;
  private revision = 0;
  private paused = false;
  private disposed = false;

  constructor(private readonly options: Options) {
    this.scheduler = options.scheduler ?? {
      now: () => Date.now(),
      setTimeout: (callback, delay) => setTimeout(callback, delay),
      clearTimeout: (timer) => clearTimeout(timer),
    };
  }

  submit(value: number): void {
    if (this.disposed) return;
    this.pending = { value, revision: ++this.revision, due: this.scheduler.now() + this.options.delay };
    this.schedule();
  }

  pause(): void {
    this.paused = true;
    this.clearTimer();
  }

  resume(): void {
    this.paused = false;
    this.schedule();
  }

  /** Engine changes wait for an already-sent gain before changing the engine. */
  async waitForActive(): Promise<void> {
    await this.active;
  }

  cancel(): void {
    ++this.revision;
    this.pending = null;
    this.clearTimer();
  }

  dispose(): void {
    this.disposed = true;
    this.cancel();
  }

  private clearTimer(): void {
    if (this.timer !== null) this.scheduler.clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(): void {
    this.clearTimer();
    if (this.disposed || this.paused || this.active || !this.pending) return;
    this.timer = this.scheduler.setTimeout(() => this.flush(), Math.max(0, this.pending.due - this.scheduler.now()));
  }

  private flush(): void {
    this.timer = null;
    if (this.disposed || this.paused || this.active || !this.pending) return;
    const request = this.pending;
    this.pending = null;
    this.active = Promise.resolve()
      .then(() => this.options.write(request.value))
      .then((value) => {
        if (!this.disposed) this.options.onCommit(value, request.revision === this.revision);
      }, (error: unknown) => {
        if (!this.disposed) this.options.onError(error, request.revision === this.revision);
      })
      .finally(() => {
        this.active = null;
        this.schedule();
      });
  }
}
