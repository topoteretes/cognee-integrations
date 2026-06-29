declare module "openclaw/plugin-sdk" {
  export type OpenClawConfig = any;

  export interface OpenClawPluginApi {
    id: string;
    name: string;
    description?: string;
    kind: "memory" | string;
    pluginConfig?: any;
    runtime?: any;
    logger: {
      info?: (msg: string) => void;
      warn?: (msg: string) => void;
      debug?: (msg: string) => void;
    };
    registerCli: (cb: (ctx: any) => void, opts?: any) => void;
    registerService: (service: any) => void;
    on: (event: string, cb: (...args: any[]) => any) => void;
  }
}
