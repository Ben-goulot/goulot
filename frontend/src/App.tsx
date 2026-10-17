import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, exchange, hasVaultToken, loadSession, setVaultToken, type VaultStatus } from "./api";
import { assertionToJson, creationOptions, parseFragment, registrationToJson, requestOptions } from "./webauthn";
import { AddMaterial } from "./chain/AddMaterial";
import { ChainView } from "./chain/ChainView";
import type { Material } from "./chain/types";

const POLL_MS = 60_000;
const HIDDEN_LOCK_MS = 5 * 60_000;

type Passkey = { id: string; label: string; created_at: string; last_used_at: string | null };
type Page = "accueil" | "coffre" | "ajout" | `chaine/${string}`;

const CHAIN_PATH = /^\/chaine\/([0-9a-f-]{36})$/;

function pageFromPath(): Page {
  const path = window.location.pathname;
  if (path.startsWith("/coffre")) return "coffre";
  if (path === "/ajout") return "ajout";
  const m = CHAIN_PATH.exec(path);
  return m ? `chaine/${m[1]}` : "accueil";
}

export default function App() {
  const [ready, setReady] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const [page, setPage] = useState<Page>(pageFromPath());
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const grant = useRef<string | undefined>(undefined);
  const hiddenSince = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api.get<VaultStatus>("/api/vault/status");
      if (!s.unlocked) setVaultToken(null);
      setStatus(s);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setFatal(e.message);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        if (window.location.pathname === "/auth") {
          const { token, grant: g } = parseFragment(window.location.hash);
          history.replaceState(null, "", g ? "/coffre" : "/");
          if (!token) throw new ApiError(401, "Lien incomplet : relancez « ./bin/goulot open ».");
          await exchange(token);
          grant.current = g;
          setPage(g ? "coffre" : "accueil");
        }
        await loadSession();
        await refresh();
        setReady(true);
      } catch (e) {
        setFatal(e instanceof Error ? e.message : "Erreur.");
      }
    })();
  }, [refresh]);

  useEffect(() => {
    const id = window.setInterval(refresh, POLL_MS); // polling never touches /api/private/
    const onVisibility = () => {
      if (document.hidden) {
        hiddenSince.current = Date.now();
      } else if (hiddenSince.current && Date.now() - hiddenSince.current > HIDDEN_LOCK_MS && hasVaultToken()) {
        void lock();
      } else {
        hiddenSince.current = null;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  });

  const go = (p: Page) => {
    history.pushState(null, "", p === "accueil" ? "/" : `/${p}`);
    setPage(p);
  };

  useEffect(() => {
    const onPop = () => setPage(pageFromPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const unlock = async () => {
    setMessage(null);
    try {
      const { options } = await api.post<{ options: Record<string, unknown> }>("/api/vault/unlock/options");
      const cred = (await navigator.credentials.get({ publicKey: requestOptions(options) })) as PublicKeyCredential;
      const res = await api.post<{ vault_token: string }>("/api/vault/unlock/verify", {
        credential: assertionToJson(cred),
      });
      setVaultToken(res.vault_token);
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Ouverture annulée.");
    }
  };

  const lock = async () => {
    setVaultToken(null);
    try {
      await api.post("/api/vault/lock");
    } finally {
      await refresh();
    }
  };

  if (fatal) {
    return (
      <main className="center">
        <h1>Goulot</h1>
        <p className="panel">{fatal}</p>
        <p className="muted">Dans le Terminal : « ./bin/goulot open ».</p>
      </main>
    );
  }
  if (!ready || !status) return <main className="center muted">Chargement…</main>;

  return (
    <>
      <header className="topbar">
        <span className="brand">Goulot</span>
        <nav>
          <button className={page === "accueil" || page.startsWith("chaine") ? "tab active" : "tab"} onClick={() => go("accueil")}>
            Marchés
          </button>
          <button className={page === "ajout" ? "tab active" : "tab"} onClick={() => go("ajout")}>
            Ajouter une matière
          </button>
          <button className={page === "coffre" ? "tab active" : "tab"} onClick={() => go("coffre")}>
            Réglages du coffre
          </button>
        </nav>
        <VaultPill status={status} onUnlock={unlock} onLock={lock} />
      </header>
      {message && <p className="banner">{message}</p>}
      <main className={page.startsWith("chaine/") ? "content wide" : "content"}>
        {page === "accueil" && <Home onOpen={(id) => go(`chaine/${id}`)} onAdd={() => go("ajout")} />}
        {page === "ajout" && <AddMaterial onOpenChain={(id) => go(`chaine/${id}`)} />}
        {page.startsWith("chaine/") && <ChainView materialId={page.slice(7)} onBack={() => go("accueil")} />}
        {page === "coffre" && (
          <VaultPage status={status} grant={grant} refresh={refresh} onUnlock={unlock} setMessage={setMessage} />
        )}
      </main>
    </>
  );
}

function VaultPill(props: { status: VaultStatus; onUnlock: () => void; onLock: () => void }) {
  const { status } = props;
  if (!status.configured || status.passkeys === 0) {
    return <span className="pill closed">Coffre non prêt</span>;
  }
  if (status.unlocked) {
    const minutes = Math.max(1, Math.ceil(status.expires_in / 60));
    return (
      <button className="pill open" onClick={props.onLock} title="Fermer le coffre">
        🔓 Coffre ouvert · {minutes} min
      </button>
    );
  }
  return (
    <button className="pill closed" onClick={props.onUnlock} title="Ouvrir avec Touch ID">
      🔒 Coffre fermé
    </button>
  );
}

const STATUS: Record<Material["status"], string> = {
  draft: "à reconstruire",
  reconstructing: "reconstruction en cours",
  review: "à relire",
  validated: "chaîne validée",
  archived: "archivée",
};

function Home(props: { onOpen: (id: string) => void; onAdd: () => void }) {
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [materials, setMaterials] = useState<Material[]>([]);
  useEffect(() => {
    api.get<Record<string, string>>("/api/settings/secrets").then(setSecrets, () => undefined);
    api.get<Material[]>("/api/materials").then(setMaterials, () => undefined);
  }, []);
  return (
    <section className="panel">
      <h2>Marchés</h2>
      <p className="muted">Les verdicts arrivent avec la veille (phase 3). Phase 2 : reconstruction des chaînes.</p>
      <h3>Matières</h3>
      {materials.length === 0 ? (
        <p className="muted">Aucune matière pour l'instant.</p>
      ) : (
        <ul className="plain">
          {materials.map((m) => (
            <li key={m.id}>
              <button className="list-item" onClick={() => props.onOpen(m.id)} disabled={m.status === "draft" || m.status === "reconstructing"}>
                <span>{m.name}</span>
                <span className="mono muted">{m.cas}</span>
                <span className="muted">{STATUS[m.status]}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <button className="primary" onClick={props.onAdd}>
        Ajouter une matière
      </button>
      <h3>Secrets du trousseau</h3>
      <ul className="plain">
        {Object.entries(secrets).map(([name, state]) => (
          <li key={name}>
            <span className="mono">{name}</span> : {state}
          </li>
        ))}
      </ul>
    </section>
  );
}

function VaultPage(props: {
  status: VaultStatus;
  grant: React.RefObject<string | undefined>;
  refresh: () => Promise<void>;
  onUnlock: () => Promise<void>;
  setMessage: (m: string | null) => void;
}) {
  const { status, grant, refresh, setMessage } = props;
  const [passkeys, setPasskeys] = useState<Passkey[] | null>(null);
  const [label, setLabel] = useState("Touch ID de ce Mac");
  const [rekeyCode, setRekeyCode] = useState<string | null>(null);

  useEffect(() => {
    if (status.unlocked) {
      api.get<Passkey[]>("/api/private/passkeys").then(setPasskeys, () => setPasskeys(null));
    } else {
      setPasskeys(null); // vault closed: private data leaves the page's memory
      setRekeyCode(null);
    }
  }, [status.unlocked]);

  const enroll = async () => {
    setMessage(null);
    try {
      const g = grant.current;
      const { options } = await api.post<{ options: Record<string, unknown> }>(
        "/api/vault/passkeys/register/options",
        { grant: g ?? null },
      );
      const cred = (await navigator.credentials.create({ publicKey: creationOptions(options) })) as PublicKeyCredential;
      await api.post("/api/vault/passkeys/register/verify", {
        credential: registrationToJson(cred),
        label,
        grant: g ?? null,
      });
      grant.current = undefined;
      setMessage("Clé d'accès enregistrée.");
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Enregistrement annulé.");
    }
  };

  const revoke = async (id: string) => {
    try {
      await api.post(`/api/private/passkeys/${encodeURIComponent(id)}/revoke`);
      setPasskeys(await api.get<Passkey[]>("/api/private/passkeys"));
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Erreur.");
    }
  };

  const setMinutes = async (minutes: number) => {
    try {
      await api.put("/api/private/vault/lock-minutes", { minutes });
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Erreur.");
    }
  };

  const canEnroll = Boolean(grant.current) || (status.unlocked && status.passkeys > 0);

  return (
    <section className="panel">
      <h2>Coffre</h2>
      <ul className="plain">
        <li>Coffre configuré : {status.configured ? "oui" : "non (« ./bin/goulot setup »)"}</li>
        <li>Clé de secours confirmée : {status.recovery_confirmed ? "oui" : "non"}</li>
        <li>Clés d'accès : {status.passkeys}</li>
        <li>Fermeture après {status.lock_minutes} min sans action (au plus 8 h, et à la mise en veille)</li>
        {status.blocked_for > 0 && <li className="warn">Ouverture bloquée encore {Math.ceil(status.blocked_for / 60)} min</li>}
      </ul>

      <h3>Ajouter une clé d'accès</h3>
      {canEnroll ? (
        <div className="row">
          <input value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} aria-label="Nom de la clé" />
          <button className="primary" onClick={enroll}>
            Enregistrer avec Touch ID
          </button>
        </div>
      ) : (
        <p className="muted">
          {status.passkeys === 0
            ? "Première clé : dans le Terminal, « ./bin/goulot vault add-passkey » (la clé de secours y est demandée)."
            : "Ouvrez le coffre avec une clé existante (moins de 2 minutes avant), ou passez par « ./bin/goulot vault add-passkey »."}
        </p>
      )}

      {status.unlocked && passkeys && (
        <>
          <h3>Clés enregistrées</h3>
          <ul className="plain">
            {passkeys.map((p) => (
              <li key={p.id} className="row">
                <span>{p.label}</span>
                <span className="muted mono">{p.created_at.slice(0, 10)}</span>
                <button onClick={() => revoke(p.id)}>Révoquer</button>
              </li>
            ))}
          </ul>
          <h3>Délai de fermeture</h3>
          <div className="row">
            {[5, 15, 30, 60].map((m) => (
              <button key={m} className={m === status.lock_minutes ? "tab active" : "tab"} onClick={() => setMinutes(m)}>
                {m} min
              </button>
            ))}
          </div>
          <h3>Nouvelle clé de secours</h3>
          {rekeyCode ? (
            <p>
              Dans le Terminal : « ./bin/goulot vault new-recovery-key », Entrée à la première question, puis ce code (10 min) :{" "}
              <span className="mono">{rekeyCode}</span>
            </p>
          ) : (
            <button onClick={async () => setRekeyCode((await api.post<{ code: string }>("/api/private/vault/rekey-grant")).code)}>
              Obtenir un code pour le Terminal
            </button>
          )}
        </>
      )}
      {!status.unlocked && status.passkeys > 0 && (
        <p className="muted">Coffre fermé : ouvrez-le avec le cadenas pour gérer les clés.</p>
      )}
    </section>
  );
}
