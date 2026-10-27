import { useState } from "react";
import { api, ApiError } from "../api";
import { Locked, usePrivacy } from "../private/privacy";

/** « Supprimer » : final; the typed name confirms it, the vault erases Ma chaîne with it. The ledger stays. */
export function DeleteMaterial(props: { materialId: string; name: string; onDone: () => void }) {
  const { vaultOpen } = usePrivacy();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  if (!open) return <button className="danger" onClick={() => setOpen(true)}>Supprimer…</button>;
  if (!vaultOpen) return <Locked text="Pour supprimer une matière, ouvre le coffre : sa Ma chaîne, ses signaux et ses devis sont effacés avec elle." />;
  const remove = async () => {
    try {
      await api.post(`/api/private/materials/${props.materialId}/delete`, { name: typed });
      props.onDone();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };
  return (
    <div className="form">
      <p>
        Suppression définitive : chaîne, règles, liens de prix et cas comparables. Les conseils passés restent au registre, marqués « matière
        supprimée ». Tape le nom de la matière pour confirmer.
      </p>
      <input value={typed} maxLength={300} onChange={(e) => setTyped(e.target.value)} aria-label="Nom de la matière à supprimer" />
      {message && <p className="warn">{message}</p>}
      <div className="row">
        <button className="danger" disabled={typed.trim().toLowerCase() !== props.name.trim().toLowerCase()} onClick={remove}>
          Supprimer définitivement
        </button>
        <button onClick={() => setOpen(false)}>Annuler</button>
      </div>
    </div>
  );
}

export async function setWatched(materialId: string, watched: boolean): Promise<string> {
  try {
    await api.post(`/api/materials/${materialId}/${watched ? "resume" : "archive"}`);
    return watched ? "Suivi repris." : "Suivi arrêté : plus de requête ni d'appel IA pour cette matière.";
  } catch (e) {
    return e instanceof ApiError ? e.message : "Erreur.";
  }
}
