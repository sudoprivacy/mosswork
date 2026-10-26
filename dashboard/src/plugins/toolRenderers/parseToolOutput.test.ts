import { describe, expect, it } from "vitest";
import {
  mergePatchedToolOutput,
  parseMossworkToolOutput,
  resolvePluginUiData,
} from "./parseToolOutput";
import {
  clearToolRenderers,
  getToolRendererVersion,
  registerToolRenderer,
  resolveToolRenderer,
} from "./registry";
import type { ToolRenderProps } from "./types";

function Dummy(_: ToolRenderProps) {
  return null;
}

describe("parseMossworkToolOutput", () => {
  it("parses mosswork_ui envelope", () => {
    const raw = JSON.stringify({
      mosswork_ui: { renderer: "demo_card", version: 1 },
      data: { count: 2 },
      text: "hi",
    });
    const parsed = parseMossworkToolOutput(raw);
    expect(parsed.isJson).toBe(true);
    expect(parsed.mossworkUi).toEqual({ renderer: "demo_card", version: 1 });
    expect(parsed.data).toEqual({ count: 2 });
    expect(parsed.text).toBe("hi");
  });

  it("keeps plain text", () => {
    const parsed = parseMossworkToolOutput("hello");
    expect(parsed.isJson).toBe(false);
    expect(parsed.text).toBe("hello");
  });
});

describe("mergePatchedToolOutput", () => {
  it("merges data into existing envelope", () => {
    const prev = JSON.stringify({
      mosswork_ui: { renderer: "demo_card" },
      data: { count: 1 },
      text: "x",
    });
    const next = mergePatchedToolOutput(prev, { count: 3 });
    expect(JSON.parse(next)).toEqual({
      mosswork_ui: { renderer: "demo_card" },
      data: { count: 3 },
      text: "x",
    });
  });
});

describe("resolvePluginUiData", () => {
  const artifact = { results: [{ season_id: 1 }] };

  it("resolves data_ref envelopes from the artifact", () => {
    const parsed = parseMossworkToolOutput(
      JSON.stringify({
        mosswork_ui: { renderer: "bilibili_player", version: 1 },
        data_ref: "artifact",
        text: "找到 1 部番剧。",
      }),
    );
    expect(parsed.data).toBeUndefined();
    expect(resolvePluginUiData(parsed, artifact, undefined)).toBe(artifact);
  });

  it("explicit data wins over data_ref/artifact (patched state)", () => {
    const patched = mergePatchedToolOutput(
      JSON.stringify({
        mosswork_ui: { renderer: "bilibili_player" },
        data_ref: "artifact",
      }),
      { results: [], current_episode: 5 },
    );
    const parsed = parseMossworkToolOutput(patched);
    const data = resolvePluginUiData(parsed, artifact, undefined) as {
      current_episode: number;
    };
    expect(data.current_episode).toBe(5);
  });

  it("falls back to legacy shapes without data_ref", () => {
    const inline = parseMossworkToolOutput(
      JSON.stringify({
        mosswork_ui: { renderer: "demo_card" },
        data: { count: 2 },
      }),
    );
    expect(resolvePluginUiData(inline, artifact, undefined)).toEqual({
      count: 2,
    });

    const plainJson = parseMossworkToolOutput(JSON.stringify({ foo: 1 }));
    expect(resolvePluginUiData(plainJson, artifact, undefined)).toEqual({
      foo: 1,
    });

    const text = parseMossworkToolOutput("hello");
    expect(resolvePluginUiData(text, artifact, "hello")).toBe("hello");
  });

  it("keeps the slim envelope when artifact is missing", () => {
    const parsed = parseMossworkToolOutput(
      JSON.stringify({
        mosswork_ui: { renderer: "bilibili_player" },
        data_ref: "artifact",
      }),
    );
    const resolved = resolvePluginUiData(parsed, undefined, undefined) as {
      data_ref: string;
    };
    expect(resolved.data_ref).toBe("artifact");
  });
});

describe("resolveToolRenderer", () => {
  it("matches by mosswork_ui.renderer then tool name", () => {
    clearToolRenderers();
    registerToolRenderer({
      id: "demo_card",
      pluginId: "demo-ui-card",
      tools: ["demo_ui_card"],
      component: Dummy,
    });
    const byHint = resolveToolRenderer({
      toolName: "other",
      pluginId: "demo-ui-card",
      parsed: parseMossworkToolOutput(
        JSON.stringify({ mosswork_ui: { renderer: "demo_card" }, data: {} }),
      ),
    });
    expect(byHint?.id).toBe("demo_card");
    const byTool = resolveToolRenderer({
      toolName: "demo_ui_card",
      parsed: parseMossworkToolOutput("plain"),
    });
    expect(byTool?.id).toBe("demo_card");
    clearToolRenderers();
  });

  it("bumps version on register so subscribers can refresh", () => {
    clearToolRenderers();
    const before = getToolRendererVersion();
    registerToolRenderer({
      id: "x",
      pluginId: "p",
      component: Dummy,
    });
    expect(getToolRendererVersion()).toBeGreaterThan(before);
    clearToolRenderers();
  });
});
