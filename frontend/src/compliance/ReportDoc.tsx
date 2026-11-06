import { Pv } from "../private/privacy";
import { fmtInt, fmtPct, fmtWeeks, footerText, frDate, groupedFingerprint, runningMarks } from "./marks";
import type { MaterialReport, Report, Risk } from "./types";

// The report itself, as printed: one table whose header (Brouillon, Confidentiel) and footer (fingerprint,
// disclaimer) the browser repeats on every page. Texts from the chain are external data, shown as plain text;
// source addresses are shown, never turned into links.

function Refs(props: { refs: string[] }) {
  if (!props.refs.length) return null;
  return <span className="report-refs"> [{props.refs.join(", ")}]</span>;
}

function RiskList(props: { title: string; items: Risk[]; empty?: string }) {
  if (!props.items.length && !props.empty) return null;
  return (
    <div className="report-factor">
      <h5>{props.title}</h5>
      {props.items.length ? (
        <ul>
          {props.items.map((r, i) => (
            <li key={i}>
              {r.title ? <strong>{r.title} : </strong> : null}
              {r.description}
              <Refs refs={r.sources} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="report-muted">{props.empty}</p>
      )}
    </div>
  );
}

function Mapping(props: { m: MaterialReport; confidential: boolean }) {
  const { m } = props;
  return (
    <div className="report-material">
      <h4>
        {m.name} <span className="report-muted">CAS {m.cas} · chaîne validée le {frDate(m.chain_validated_at)}</span>
      </h4>
      <p className="report-muted">
        {m.strategic.listed
          ? `Matière première stratégique d'après son nom (${m.strategic.entry}), grade à confirmer : ${m.strategic.source}.`
          : "Hors de la liste des matières premières stratégiques d'après son nom : évaluation volontaire."}
      </p>
      <table className="report-table">
        <thead>
          <tr>
            <th>Étape</th>
            <th>Niveau</th>
            <th>Pays et parts mondiales</th>
            <th>Sites cités</th>
            <th>Confiance</th>
          </tr>
        </thead>
        <tbody>
          {m.mapping.map((st) =>
            st.documented ? (
              st.levels.map((lvl, i) => (
                <tr key={`${st.stage}-${lvl.tier}`}>
                  <td>{i === 0 ? st.label : ""}</td>
                  <td>{lvl.products.join(", ") || `niveau ${lvl.tier}`}</td>
                  <td>
                    {lvl.countries.length
                      ? lvl.countries.map((c) => `${c.country_name} ${fmtPct(c.share_pct)}`).join(" · ")
                      : "parts non documentées"}
                  </td>
                  <td>
                    {lvl.sites.filter((s) => s.type !== "pays").map((s) => s.name).join(" · ") || "—"}
                    <Refs refs={lvl.sources} />
                  </td>
                  <td>{lvl.confidence ?? "—"}</td>
                </tr>
              ))
            ) : (
              <tr key={st.stage}>
                <td>{st.label}</td>
                <td colSpan={4} className="report-muted">
                  Étape non documentée dans la chaîne validée.
                </td>
              </tr>
            ),
          )}
        </tbody>
      </table>
      {props.confidential && m.my_chain && (
        <>
          <h5>Ma chaîne : sites déclarés et part du volume</h5>
          <table className="report-table">
            <thead>
              <tr>
                <th>Site</th>
                <th>Rôle</th>
                <th>Lieu</th>
                <th>Part du volume</th>
                <th>Stock</th>
              </tr>
            </thead>
            <tbody>
              {m.my_chain.sites.map((s, i) => (
                <tr key={i}>
                  <td><Pv>{s.name}{s.company ? ` (${s.company})` : ""}</Pv></td>
                  <td>{s.role}</td>
                  <td><Pv>{[s.city, s.country_name].filter(Boolean).join(", ") || "—"}</Pv></td>
                  <td><Pv>{fmtPct(s.exposure_pct)}</Pv></td>
                  <td><Pv>{s.stock_days === null ? "—" : `${s.stock_days} j`}</Pv></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {props.confidential && m.my_chain === null && <p className="report-warn">Ma chaîne n'est pas saisie pour cette matière.</p>}
    </div>
  );
}

function Concentration(props: { report: Report }) {
  const rows = props.report.materials.flatMap((m) => [
    ...m.concentration.map((r) => ({ m, label: `${r.stage_label} (${r.products.join(", ") || `niveau ${r.tier}`})`, r })),
    ...(m.my_chain ? [{ m, label: "Ma chaîne (production)", r: { ...m.my_chain.concentration, stage: "", stage_label: "", tier: 0, products: [] } }] : []),
  ]);
  return (
    <table className="report-table">
      <thead>
        <tr>
          <th>Matière</th>
          <th>Étape</th>
          <th>Premier pays</th>
          <th>Indice HHI</th>
          <th>Repère européen de 65 %</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ m, label, r }, i) => (
          <tr key={i}>
            <td>{m.name}</td>
            <td>{label}</td>
            <td>
              {r.top_country_name ?? "—"} {fmtPct(r.top_share_pct)}
              {r.inconsistent && <span className="report-warn"> (parts au-delà de 100 %)</span>}
            </td>
            <td>
              {fmtInt(r.hhi)}
              {r.hhi_band ? ` (${r.hhi_band})` : ""}
              {r.lower_bound && r.hhi !== null && <span className="report-muted"> minimum, parts connues {fmtPct(r.coverage_pct)}</span>}
            </td>
            <td>
              <span className={`report-bench bench-${r.benchmark.status.replace(/[^a-z]/g, "")}`}>{r.benchmark.status}</span>
              {r.benchmark.country_name ? ` ${r.benchmark.country_name} ${fmtPct(r.benchmark.share_pct)}` : ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Factors(props: { m: MaterialReport }) {
  const f = props.m.factors;
  return (
    <div className="report-material">
      <h4>{props.m.name}</h4>
      <RiskList title="Contrôles d'exportation" items={f.export_controls} empty="Aucun relevé dans la chaîne validée." />
      <RiskList title="Mesures commerciales et réglementaires" items={f.trade_measures} empty="Aucune relevée dans la chaîne validée." />
      <div className="report-factor">
        <h5>Détroits et voies traversés</h5>
        {f.chokepoints.passages.length ? <p>{f.chokepoints.passages.map((p) => p.name).join(" · ")}</p> : null}
        {f.chokepoints.risks.length ? (
          <ul>
            {f.chokepoints.risks.map((r, i) => (
              <li key={i}>{r.title ? <strong>{r.title} : </strong> : null}{r.description}<Refs refs={r.sources} /></li>
            ))}
          </ul>
        ) : null}
        {!f.chokepoints.passages.length && !f.chokepoints.risks.length && <p className="report-muted">Aucun relevé.</p>}
      </div>
      <RiskList title="Dépendances de coproduits" items={f.coproduct_dependencies} empty="Aucune relevée." />
      <RiskList title="Autres risques structurels" items={f.other_structural} />
      <div className="report-factor">
        <h5>Événements des 12 derniers mois</h5>
        {f.events_12_months.length ? (
          <ul>
            {f.events_12_months.map((e) => (
              <li key={e.id}>
                {frDate(e.first_seen)} : {e.title}
                {e.place ? ` (${e.place})` : ""}
                {e.confirmed ? "" : ", signal précoce"}
              </li>
            ))}
          </ul>
        ) : (
          <p className="report-muted">Aucun événement relevé par la veille.</p>
        )}
      </div>
      <div className="report-factor">
        <h5>Goulots à venir</h5>
        {f.upcoming_bottlenecks.length ? (
          <ul>
            {f.upcoming_bottlenecks.map((d, i) => (
              <li key={i}>{d.date} : {d.what}<Refs refs={d.sources} /></li>
            ))}
          </ul>
        ) : (
          <p className="report-muted">Aucune échéance connue.</p>
        )}
      </div>
    </div>
  );
}

function Vulnerability(props: { report: Report }) {
  return (
    <table className="report-table">
      <thead>
        <tr>
          <th>Matière</th>
          <th>Délai X et propagation</th>
          <th>Stocks tampons</th>
          <th>Substituts</th>
          <th>Sources alternatives</th>
          <th>Couverture de l'entreprise</th>
        </tr>
      </thead>
      <tbody>
        {props.report.materials.map((m) => {
          const v = m.vulnerability;
          const cover = v.company_coverage;
          return (
            <tr key={m.id}>
              <td>{m.name}</td>
              <td>
                {v.verdict?.x_weeks != null ? `${v.verdict.label} à ${fmtWeeks(v.verdict.x_weeks)} ; ` : v.verdict ? `${v.verdict.label} ; ` : ""}
                {v.propagation ? `propagation ${fmtWeeks(v.propagation.weeks)} (${fmtWeeks(v.propagation.low_weeks)} à ${fmtWeeks(v.propagation.high_weeks)}) depuis ${v.propagation.from}` : "propagation non calculable"}
              </td>
              <td>{v.buffers.length ? v.buffers.map((b) => `${b.name} ≈ ${b.days} j`).join(" · ") : v.propagation?.buffers_days ? `${v.propagation.buffers_days} j sur le chemin` : "—"}</td>
              <td>{v.substitutes.length ? v.substitutes.map((s) => s.title || s.description).join(" · ") : v.context_documented ? "aucun substitut identifié" : "non documenté"}</td>
              <td>{v.alternative_sources.length ? v.alternative_sources.map((a) => `${a.country_name} ${fmtPct(a.share_pct)}`).join(" · ") : "—"}</td>
              <td>
                {cover.status === "renseignée" ? (
                  <Pv>
                    {cover.stock_weeks != null ? `stock ${fmtWeeks(cover.stock_weeks)}` : "stock non saisi"}
                    {cover.supplier_lead_time_days != null ? ` · délai ${cover.supplier_lead_time_days} j` : ""}
                  </Pv>
                ) : (
                  cover.status
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function ReportDoc(props: { report: Report }) {
  const { report } = props;
  const marks = runningMarks(report);
  const s = report.template.sections;
  const sources = report.materials.flatMap((m) => m.sources.map((src) => ({ m, src })));
  return (
    <table className={report.draft ? "report draft" : "report"}>
      <thead>
        <tr>
          <td>
            <div className="report-marks">
              {marks.map((mark) => (
                <span key={mark} className={mark.startsWith("Confidentiel") ? "report-mark confidential" : "report-mark draft"}>
                  {mark}
                </span>
              ))}
            </div>
          </td>
        </tr>
      </thead>
      <tfoot>
        <tr>
          <td>
            <div className="report-foot">
              <span>{report.template.disclaimer}</span>
              <span className="mono">{footerText(report)}</span>
            </div>
          </td>
        </tr>
      </tfoot>
      <tbody>
        <tr>
          <td>
            <section className="report-page">
              <h2>{report.template.title}</h2>
              <p className="report-muted">
                {report.company || "[Nom de l'entreprise]"} · {frDate(report.generated_at)} · {report.materials.length} matière
                {report.materials.length > 1 ? "s" : ""} · {report.basis === "my_chain" ? "fondé sur Ma chaîne" : "fondé sur la chaîne type, tirée de sources publiques"}
              </p>
              <p className="report-muted">{report.template.subtitle} · {report.template.reference}</p>
              <h3>1. {s.mapping}</h3>
              {report.materials.map((m) => (
                <Mapping key={m.id} m={m} confidential={report.confidential} />
              ))}
            </section>
            <section className="report-page">
              <h3>2. {s.factors}</h3>
              <Concentration report={report} />
              {report.materials.map((m) => (
                <Factors key={m.id} m={m} />
              ))}
            </section>
            <section className="report-page">
              <h3>3. {s.vulnerability}</h3>
              <Vulnerability report={report} />
              <h3>4. {s.mitigation}</h3>
              {report.materials.map((m) => (
                <div key={m.id} className="report-factor">
                  <h5>{m.name}</h5>
                  {m.mitigation.choices.length ? (
                    <p>
                      <Pv>
                        {m.mitigation.choices.map((c) => c.label).join(", ")}
                        {m.mitigation.comment ? ` : ${m.mitigation.comment}` : ""}
                      </Pv>
                    </p>
                  ) : (
                    <p className="report-warn">
                      {m.mitigation.status === "coffre fermé" ? "Mesures dans le coffre : ouvre-le pour les inclure." : "À compléter : choisis au moins une mesure."}
                    </p>
                  )}
                </div>
              ))}
              <p className="report-muted">Mesures choisies par l'entreprise ; Goulot ne les rédige jamais.</p>
            </section>
            <section className="report-page">
              <h3>5. {s.method}</h3>
              <p>{report.method}</p>
              <p>Fiabilité de Goulot, d'après son registre : {report.reliability.text}.</p>
              <p>
                Chaînes utilisées :{" "}
                {report.materials.map((m) => `${m.name}, validée le ${frDate(m.chain_validated_at)}`).join(" ; ")}.
              </p>
              <h5>Sources ({sources.length})</h5>
              <ol className="report-sources">
                {sources.map(({ m, src }, i) => (
                  <li key={i}>
                    <span className="report-muted">{m.name}, {src.ref} :</span> {src.title || src.publisher || "sans titre"}
                    {src.publisher && src.title ? `, ${src.publisher}` : ""}
                    {/^\d{4}/.test(src.date) ? `, ${src.date}` : ""} <span className="mono report-url">{src.url}</span>
                  </li>
                ))}
              </ol>
              <div className="report-sign">
                <p>
                  Signataire : {report.signatory.name || "[nom]"}, {report.signatory.role || "[fonction]"}
                </p>
                <p>{report.reviewed ? "Rapport relu et validé par le signataire." : "Relecture à valider."}</p>
                <p className="mono report-muted">Empreinte complète : {groupedFingerprint(report.fingerprint)}</p>
              </div>
            </section>
          </td>
        </tr>
      </tbody>
    </table>
  );
}
