import { request } from "../request";

export interface ClientDownloads {
  version: string;
  windows_url: string;
  macos_url: string;
}

// Root API
export const rootApi = {
  readRoot: () => request<unknown>("/"),
  getVersion: () => request<{ version: string }>("/version"),
  getClientDownloads: () => request<ClientDownloads>("/auth/client-downloads"),
};
