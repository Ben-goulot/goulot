import type { Report } from "./types";

// What every page of the report carries. The marks sit in the header of a table whose <thead> and <tfoot> the
// browser repeats on every printed page (docs/SPEC.md, section 10): « Brouillon » and « Confidentiel » on each page,
// the fingerprint and the disclaimer at the bottom of each page.

export function runningMarks(report: Report): string[] {
  const marks: string[] = [];
  if (report.confidential && report.confidential_label) marks.push(report.confidential_label);
  if (report.draft && report.draft_label) marks.push(report.draft_label);
  return marks;
}

export function groupedFingerprint(fingerprint: string): string {
  return (fingerprint.match(/.{1,4}/g) ?? []).join(" ");
}

export function footerText(report: Report): string {
  const seq = report.ledger_seq !== undefined ? ` · registre n° ${report.ledger_seq}` : " · aperçu, non inscrit au registre";
  return `Empreinte SHA-256 ${groupedFingerprint(report.fingerprint)}${seq}`;
}

const pct = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const int = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });

export function fmtPct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${pct.format(v)} %`;
}

export function fmtInt(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : int.format(v);
}

export function fmtWeeks(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${pct.format(v)} sem.`;
}

export function frDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

/** Saves a file built in memory: nothing goes through the server again, nothing stays in the page. */
export function save(name: string, content: string, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
