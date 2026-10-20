import { createContext, useContext, type ReactNode } from "react";

// Vault state and presentation mode, shared by every screen. A private value is rendered inside <Pv>: blurred in
// presentation mode, even vault open (design/README.md, « Mode présentation »).
export type Privacy = { vaultOpen: boolean; presenting: boolean; unlock: () => void };

export const PrivacyContext = createContext<Privacy>({ vaultOpen: false, presenting: false, unlock: () => undefined });

export function usePrivacy(): Privacy {
  return useContext(PrivacyContext);
}

export function Pv(props: { children: ReactNode }) {
  return <span className="pv">{props.children}</span>;
}

/** What a closed vault shows instead of anything private: never a hint. */
export function Locked(props: { text?: string }) {
  const { unlock } = usePrivacy();
  return (
    <div className="locked">
      <p>🔒 {props.text ?? "Ma chaîne, tes stocks, tes prix et tes signaux sont chiffrés sur ce Mac."}</p>
      <button className="primary" onClick={unlock}>
        Ouvrir avec Touch ID
      </button>
    </div>
  );
}
