import { describe, expect, it } from "vitest";
import { footerText, groupedFingerprint, runningMarks } from "./marks";
import type { Report } from "./types";

const base = {
  confidential: false,
  confidential_label: null,
  draft: false,
  draft_label: null,
  fingerprint: "4be177c0" + "0".repeat(52) + "9c02",
} as unknown as Report;

describe("marks repeated on every printed page", () => {
  it("a complete report on the typical chain carries no mark", () => {
    expect(runningMarks(base)).toEqual([]);
  });

  it("an incomplete report says « Brouillon » in the running header", () => {
    const marks = runningMarks({ ...base, draft: true, draft_label: "Brouillon : rapport incomplet" });
    expect(marks).toEqual(["Brouillon : rapport incomplet"]);
  });

  it("a report on Ma chaîne says « Confidentiel », draft or not", () => {
    const report = { ...base, confidential: true, confidential_label: "Confidentiel : fondé sur Ma chaîne" };
    expect(runningMarks(report)).toEqual(["Confidentiel : fondé sur Ma chaîne"]);
    expect(runningMarks({ ...report, draft: true, draft_label: "Brouillon : rapport incomplet" })).toEqual([
      "Confidentiel : fondé sur Ma chaîne",
      "Brouillon : rapport incomplet",
    ]);
  });

  it("the footer shows the fingerprint and the ledger entry, or says it is only a preview", () => {
    expect(groupedFingerprint("4be177c09c02")).toBe("4be1 77c0 9c02");
    expect(footerText({ ...base, ledger_seq: 215 })).toContain("registre n° 215");
    expect(footerText(base)).toContain("aperçu, non inscrit au registre");
    expect(footerText(base)).toContain("4be1 77c0");
  });
});
