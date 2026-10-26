import { request } from "../request";
import type {
  MossworkCronRow,
  MossworkCronCreateBody,
  MossworkCronPatchBody,
} from "../types";

export interface MossworkCronSettings {
  timezone: string;
}

export interface CronTaskExamples {
  zh?: string[];
  en?: string[];
}

export const mossworkCronApi = {
  settings: () => request<MossworkCronSettings>("/cron/settings"),

  list: (agentId: string) =>
    request<MossworkCronRow[]>(`/agents/${agentId}/cron`),

  create: (agentId: string, body: MossworkCronCreateBody) =>
    request<MossworkCronRow>(`/agents/${agentId}/cron`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  get: (agentId: string, cronId: string) =>
    request<MossworkCronRow>(`/agents/${agentId}/cron/${cronId}`),

  patch: (agentId: string, cronId: string, body: MossworkCronPatchBody) =>
    request<MossworkCronRow>(`/agents/${agentId}/cron/${cronId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  delete: (agentId: string, cronId: string) =>
    request<void>(`/agents/${agentId}/cron/${cronId}`, { method: "DELETE" }),

  runNow: (agentId: string, cronId: string) =>
    request<void>(`/agents/${agentId}/cron/${cronId}/run-now`, {
      method: "POST",
    }),
};
