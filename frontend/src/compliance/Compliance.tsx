import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { usePrivacy } from "../private/privacy";
import { save } from "./marks";
import { ReportDoc } from "./ReportDoc";
import type { ExportResult, Options, Profile, Report } from "./types";

// « Conformité » (docs/SPEC.md, section 10; design/maquette/Conformite.dc.html). Vault closed, the report rests on
// the typical chain and stays a draft: the mitigation choices are in the vault. Vault open, they are read and saved
// encrypted; Ma chaîne can be the basis, and the report is then « Confidentiel ». Nothing here is kept by the page:
// the annexes are built by the server, saved by the browser on request, and forgotten.

const EMPTY: Profile = { company: "", signatory_name: "", signatory_role: "", measures: {} };

export function Compliance() {
  const { vaultOpen, unlock } = usePrivacy();
  const [options, setOptions] = useState<Options | null>(null);
  const [template, setTemplate] = useState("article24");
  const [chosen, setChosen] = useState<string[]>([]);
  const [basis, setBasis] = useState<"typical" | "my_chain">("typical");
  const [profile, setProfile] = useState<Profile>(EMPTY);
  const [reviewed, setReviewed] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [exported, setExported] = useState<ExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const saveTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    api.get<Options>("/api/compliance/options").then(
      (o) => {
        setOptions(o);
        setChosen(o.materials.map((m) => m.id));
      },
      (e: unknown) => setError(e instanceof ApiError ? e.message : "Erreur."),
    );
  }, []);

  useEffect(() => {
    if (!vaultOpen) {
      setProfile(EMPTY);
      setBasis("typical");
      return;
    }
    api.get<Profile>("/api/private/compliance/profile").then(setProfile, () => undefined);
  }, [vaultOpen]);

  const body = useCallback(
    () => ({
      template,
      material_ids: chosen,
      basis: vaultOpen ? basis : "typical",
      company: profile.company,
      signatory_name: profile.signatory_name,
      signatory_role: profile.signatory_role,
      reviewed,
    }),
    [template, chosen, basis, vaultOpen, profile.company, profile.signatory_name, profile.signatory_role, reviewed],
  );

  // Preview, a moment after each change; an export already made no longer matches the screen.
  useEffect(() => {
    setExported(null);
    if (!chosen.length) {
      setReport(null);
      return;
    }
    const timer = window.setTimeout(() => {
      const path = vaultOpen ? "/api/private/compliance/preview" : "/api/compliance/preview";
      api.post<Report>(path, body()).then(
        (r) => {
          setReport(r);
          setError(null);
        },
        (e: unknown) => setError(e instanceof ApiError ? e.message : "Erreur."),
      );
    }, 350);
    return () => window.clearTimeout(timer);
  }, [body, chosen.length, vaultOpen, profile.measures]);

  const storeProfile = (next: Profile) => {
    setProfile(next);
    if (!vaultOpen) return;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      api.put("/api/private/compliance/profile", next).catch((e: unknown) => setError(e instanceof ApiError ? e.message : "Erreur."));
    }, 400);
  };

  const toggleMeasure = (materialId: string, choice: string) => {
    const current = profile.measures[materialId] ?? { choices: [], comment: "" };
    const choices = current.choices.includes(choice) ? current.choices.filter((c) => c !== choice) : [...current.choices, choice];
    storeProfile({ ...profile, measures: { ...profile.measures, [materialId]: { ...current, choices } } });
  };

  const setComment = (materialId: string, comment: string) => {
    const current = profile.measures[materialId] ?? { choices: [], comment: "" };
    storeProfile({ ...profile, measures: { ...profile.measures, [materialId]: { ...current, comment } } });
  };

  const runExport = async () => {
    setBusy(true);
    try {
      const path = vaultOpen ? "/api/private/compliance/export" : "/api/compliance/export";
      const result = await api.post<ExportResult>(path, body());
      setReport(result.report);
      setExported(result);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Erreur.");
    } finally {
      setBusy(false);
    }
  };

  if (!options) return <p className="muted">{error ?? "Chargement…"}</p>;
  const names = new Map(options.materials.map((m) => [m.id, m.name]));
  return (
    <div className="compliance">
      <div className="no-print">
        <h2>Évaluation des risques d'approvisionnement</h2>
        <p className="muted">
          Goulot assemble la cartographie, les facteurs de risque et ta vulnérabilité à partir des chaînes que tu as validées. Tu choisis les
          mesures, tu signes, tu exportes. Rien ne quitte ton Mac et aucune IA ne rédige ce rapport.
        </p>
        {error && <p className="banner">{error}</p>}
      </div>
      <div className="compliance-layout">
        <aside className="panel compliance-side no-print">
          <p className="eyebrow">Modèle</p>
          <div className="stack">
            {options.templates.map((t) => (
              <label key={t.id} className="choice">
                <input type="radio" name="template" checked={template === t.id} onChange={() => setTemplate(t.id)} />
                <span>
                  <strong>{t.title}</strong>
                  <br />
                  <span className="muted small">{t.subtitle}</span>
                </span>
              </label>
            ))}
            <p className="muted small">Devoir de vigilance : plus tard.</p>
          </div>
          <p className="eyebrow">Matières</p>
          {options.materials.length === 0 && <p className="muted">Aucune chaîne validée pour l'instant.</p>}
          <div className="stack">
            {options.materials.map((m) => (
              <label key={m.id} className="choice">
                <input
                  type="checkbox"
                  checked={chosen.includes(m.id)}
                  onChange={() => setChosen(chosen.includes(m.id) ? chosen.filter((x) => x !== m.id) : [...chosen, m.id])}
                />
                <span>
                  {m.name} <span className="muted small">CAS {m.cas} · validée le {m.validated_at ?? "—"}</span>
                </span>
              </label>
            ))}
          </div>
          <p className="eyebrow">Base de la cartographie</p>
          <div className="stack">
            <label className="choice">
              <input type="radio" name="basis" checked={basis === "typical" || !vaultOpen} onChange={() => setBasis("typical")} />
              <span>Chaîne type, tirée de sources publiques</span>
            </label>
            <label className="choice">
              <input type="radio" name="basis" disabled={!vaultOpen} checked={vaultOpen && basis === "my_chain"} onChange={() => setBasis("my_chain")} />
              <span>
                Ma chaîne <span className="muted small">(coffre ouvert ; chaque page porte « Confidentiel »)</span>
              </span>
            </label>
          </div>
          <p className="eyebrow">Mesures d'atténuation retenues</p>
          {!vaultOpen ? (
            <div className="locked">
              <p>🔒 Tes mesures sont chiffrées dans le coffre. Sans elles, le rapport reste un brouillon.</p>
              <button className="primary" onClick={unlock}>
                Ouvrir avec Touch ID
              </button>
            </div>
          ) : (
            chosen.map((id) => {
              const current = profile.measures[id] ?? { choices: [], comment: "" };
              return (
                <fieldset key={id} className="measures">
                  <legend>{names.get(id)}</legend>
                  {options.mitigations.map((c) => (
                    <label key={c.id} className="choice">
                      <input type="checkbox" checked={current.choices.includes(c.id)} onChange={() => toggleMeasure(id, c.id)} />
                      <span>{c.label}</span>
                    </label>
                  ))}
                  <label className="field">
                    <span className="muted small">Commentaire{current.choices.includes("autre") ? " (exigé pour « Autre »)" : ""}</span>
                    <textarea maxLength={1000} value={current.comment} onChange={(e) => setComment(id, e.target.value)} />
                  </label>
                </fieldset>
              );
            })
          )}
          <p className="muted small">Tu choisis : Goulot ne rédige jamais les mesures à ta place.</p>
          <p className="eyebrow">Signataire</p>
          <div className="form">
            <label className="field">
              <span className="muted small">Entreprise</span>
              <input maxLength={200} value={profile.company} onChange={(e) => storeProfile({ ...profile, company: e.target.value })} />
            </label>
            <label className="field">
              <span className="muted small">Nom</span>
              <input maxLength={120} value={profile.signatory_name} onChange={(e) => storeProfile({ ...profile, signatory_name: e.target.value })} />
            </label>
            <label className="field">
              <span className="muted small">Fonction</span>
              <input maxLength={120} value={profile.signatory_role} onChange={(e) => storeProfile({ ...profile, signatory_role: e.target.value })} />
            </label>
            {!vaultOpen && <p className="muted small">Coffre fermé : ces champs ne sont pas enregistrés.</p>}
            <label className="choice">
              <input type="checkbox" checked={reviewed} onChange={() => setReviewed(!reviewed)} />
              <span>J'ai relu le rapport et je le valide</span>
            </label>
          </div>
        </aside>
        <div className="compliance-preview">
          <p className="eyebrow no-print">Aperçu · format A4</p>
          {report ? <ReportDoc report={report} /> : <p className="muted no-print">Choisis au moins une matière.</p>}
        </div>
        <aside className="panel compliance-side no-print">
          <p className="eyebrow">Contrôle avant export</p>
          <ul className="plain checks">
            {(report?.checks ?? []).map((c) => (
              <li key={c.id} className={c.ok ? "check ok" : "check todo"}>
                <span aria-hidden="true">{c.ok ? "✓" : "!"}</span>
                <span>
                  <strong>{c.label}</strong>
                  <br />
                  <span className="muted small">{c.detail}</span>
                </span>
              </li>
            ))}
          </ul>
          {report?.draft && <p className="warn small">Incomplet : chaque page portera « Brouillon ».</p>}
          <button className="primary" disabled={!report || busy} onClick={() => void runExport()}>
            {busy ? "Export…" : "Exporter et inscrire au registre"}
          </button>
          {exported && (
            <div className="stack">
              <p className="small">
                Inscrit au registre, n° {exported.report.ledger_seq}.
              </p>
              <button onClick={() => window.print()}>Imprimer le PDF</button>
              <button onClick={() => save(exported.files.json.name, exported.files.json.content, "application/json")}>Annexe JSON</button>
              <button onClick={() => save(exported.files.csv.name, exported.files.csv.content, "text/csv;charset=utf-8")}>Annexe CSV</button>
            </div>
          )}
          <p className="muted small">
            Le rapport et ses annexes sont inscrits au registre avec leur empreinte : tu pourras prouver plus tard ce que tu savais à cette date.
            {basis === "my_chain" && vaultOpen ? " Fondé sur Ma chaîne, il n'est gardé nulle part sur ce Mac : seule son empreinte l'est." : ""}
          </p>
        </aside>
      </div>
    </div>
  );
}
