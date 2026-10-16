// WebAuthn helpers: the server sends options as JSON (base64url), the browser API wants ArrayBuffers.

export function b64urlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const b64 = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const bin = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function bytesToB64url(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type JsonDescriptor = { id: string; type: "public-key"; transports?: AuthenticatorTransport[] };

export function creationOptions(json: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const o = json as unknown as PublicKeyCredentialCreationOptions & {
    challenge: string;
    user: { id: string; name: string; displayName: string };
  };
  return {
    ...o,
    challenge: b64urlToBytes(o.challenge),
    user: { ...o.user, id: b64urlToBytes(o.user.id) },
    excludeCredentials: ((json.excludeCredentials ?? []) as JsonDescriptor[]).map((c) => ({ ...c, id: b64urlToBytes(c.id) })),
  };
}

export function requestOptions(json: Record<string, unknown>): PublicKeyCredentialRequestOptions {
  const o = json as unknown as PublicKeyCredentialRequestOptions & {
    challenge: string;
  };
  return {
    ...o,
    challenge: b64urlToBytes(o.challenge),
    allowCredentials: ((json.allowCredentials ?? []) as JsonDescriptor[]).map((c) => ({ ...c, id: b64urlToBytes(c.id) })),
  };
}

export function registrationToJson(cred: PublicKeyCredential): Record<string, unknown> {
  const r = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: bytesToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bytesToB64url(r.clientDataJSON),
      attestationObject: bytesToB64url(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
    clientExtensionResults: {},
  };
}

export function assertionToJson(cred: PublicKeyCredential): Record<string, unknown> {
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bytesToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bytesToB64url(r.clientDataJSON),
      authenticatorData: bytesToB64url(r.authenticatorData),
      signature: bytesToB64url(r.signature),
      userHandle: r.userHandle ? bytesToB64url(r.userHandle) : null,
    },
    clientExtensionResults: {},
  };
}

// Launch link fragment: #t=<login token>&g=<enrollment grant>. Read once, then removed from the address bar.
export function parseFragment(hash: string): { token?: string; grant?: string } {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  return { token: params.get("t") ?? undefined, grant: params.get("g") ?? undefined };
}
