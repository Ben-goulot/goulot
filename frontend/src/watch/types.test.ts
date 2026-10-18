import { describe, expect, it } from "vitest";
import { RANK_LABEL, RESULT_LABEL, signed, verdictClass, type Verdict } from "./types";

const verdict = (v: Verdict["verdict"]) => ({ verdict: v }) as Verdict;

describe("watch display helpers", () => {
  it("always signs variations, with a French decimal comma", () => {
    expect(signed(4.56)).toBe("+4,6");
    expect(signed(-2)).toBe("-2,0");
    expect(signed(null)).toBe("—");
  });
  it("gives each verdict its own class, never color alone", () => {
    expect(verdictClass(verdict("ant"))).toBe("badge ant");
    expect(verdictClass(verdict("pot"))).toBe("badge pot");
    expect(verdictClass(verdict("unverified"))).toBe("badge unverified");
    expect(verdictClass(null)).toBe("badge neutral");
  });
  it("names every series rank and outcome in French", () => {
    expect(Object.keys(RANK_LABEL)).toHaveLength(6);
    expect(RESULT_LABEL.unresolved).toBe("non résolu");
  });
});
