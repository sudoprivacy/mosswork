import { describe, expect, it } from "vitest";
import { createInstance } from "i18next";
import en from "../../../locales/en.json";
import zh from "../../../locales/zh.json";
import manifest from "../../../../../src/octop/infra/agents/experts/library/general-assistant/manifest.json";
import { presetToCron } from "./components/constants";

import {
  exampleFormValues,
  normalizeTaskExampleCount,
  resolveTaskExamples,
  taskExampleColumns,
} from "./taskExamples";

const defaults = ["default-a", "default-b"];

describe("exampleFormValues", () => {
  const translations = createInstance();
  void translations.init({
    lng: "en",
    resources: { en: { translation: en }, zh: { translation: zh } },
  });

  it.each(["zh", "en"] as const)(
    "opens each %s general-assistant card with its schedule",
    (locale) => {
      const expected = ["0 9 * * 1-5", "0 20 * * 0", "0 22 * * *"];
      manifest.task_examples[locale].forEach((text, index) => {
        const draft = exampleFormValues(text, translations.t);
        expect(presetToCron(draft._preset!)).toBe(expected[index]);
        expect(draft).toMatchObject({
          enabled: true,
          task_type: index === 2 ? "text" : "agent",
        });
        expect(draft.prompt).toBeTruthy();
        expect(draft.prompt).not.toBe(text);
        expect(draft.name).toBeTruthy();
      });
    },
  );

  it("leaves the schedule unset for a custom expert example", () => {
    const text = "Review today's unresolved tickets";
    expect(exampleFormValues(text, translations.t)).toMatchObject({
      name: text,
      prompt: text,
      _preset: undefined,
      task_type: "agent",
    });
  });
});

describe("taskExampleColumns", () => {
  it("uses one column unless there are six cards", () => {
    expect(taskExampleColumns(0)).toBe(1);
    expect(taskExampleColumns(3)).toBe(1);
    expect(taskExampleColumns(5)).toBe(1);
  });

  it("uses two columns for a full six-card set", () => {
    expect(taskExampleColumns(6)).toBe(2);
  });
});

describe("normalizeTaskExampleCount", () => {
  it("keeps three or fewer items", () => {
    expect(normalizeTaskExampleCount(["a", "b"])).toEqual(["a", "b"]);
  });

  it("truncates four or five items to three", () => {
    expect(normalizeTaskExampleCount(["a", "b", "c", "d", "e"])).toEqual([
      "a",
      "b",
      "c",
    ]);
  });

  it("caps extras at six", () => {
    expect(
      normalizeTaskExampleCount(["a", "b", "c", "d", "e", "f", "g"]),
    ).toEqual(["a", "b", "c", "d", "e", "f"]);
  });
});

describe("resolveTaskExamples", () => {
  it("uses defaults when the manifest field is missing", () => {
    expect(resolveTaskExamples(null, "zh", defaults)).toEqual(defaults);
    expect(resolveTaskExamples(undefined, "en", defaults)).toEqual(defaults);
  });

  it("uses the active locale list when present", () => {
    expect(
      resolveTaskExamples({ zh: ["中文"], en: ["English"] }, "zh", defaults),
    ).toEqual(["中文"]);
    expect(
      resolveTaskExamples({ zh: ["中文"], en: ["English"] }, "en", defaults),
    ).toEqual(["English"]);
  });

  it("falls back to the other locale when the active list is empty", () => {
    expect(
      resolveTaskExamples({ zh: ["中文"], en: [] }, "en", defaults),
    ).toEqual(["中文"]);
  });

  it("shows no cards when the field exists but both lists are empty", () => {
    expect(resolveTaskExamples({ zh: [], en: [] }, "zh", defaults)).toEqual([]);
    expect(resolveTaskExamples({}, "en", defaults)).toEqual([]);
  });

  it("truncates four or five locale items to three", () => {
    expect(
      resolveTaskExamples(
        { zh: ["一", "二", "三", "四", "五"], en: ["a", "b", "c", "d"] },
        "zh",
        defaults,
      ),
    ).toEqual(["一", "二", "三"]);
  });
});
