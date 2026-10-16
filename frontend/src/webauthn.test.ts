import { describe, expect, it } from "vitest";
import { b64urlToBytes, bytesToB64url, parseFragment, requestOptions } from "./webauthn";

describe("webauthn helpers", () => {
  it("round-trips base64url", () => {
    const bytes = new Uint8Array([0, 1, 250, 251, 252, 253, 254, 255]);
    expect(Array.from(b64urlToBytes(bytesToB64url(bytes)))).toEqual(Array.from(bytes));
    expect(bytesToB64url(bytes)).not.toMatch(/[+/=]/);
  });

  it("converts request options to buffers", () => {
    const opts = requestOptions({ challenge: "AAEC", allowCredentials: [{ id: "AwQ", type: "public-key" }] });
    expect(Array.from(opts.challenge as Uint8Array)).toEqual([0, 1, 2]);
    expect(Array.from(opts.allowCredentials![0].id as Uint8Array)).toEqual([3, 4]);
  });

  it("reads the launch fragment", () => {
    expect(parseFragment("#t=abc&g=def")).toEqual({ token: "abc", grant: "def" });
    expect(parseFragment("")).toEqual({ token: undefined, grant: undefined });
  });
});
