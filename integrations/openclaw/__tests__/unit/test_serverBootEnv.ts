/**
 * Default-user credentials for the plugin-managed local server (SDK-740).
 *
 * cognee >= 1.6.0 creates no default user unless DEFAULT_USER_PASSWORD is set at
 * startup, and a password-less user cannot log in. The plugin's API key is minted
 * by a one-time JWT login as that user, so `ensure_and_boot.py` (inlined as
 * ENSURE_SCRIPT_CONTENT) `setdefault`s the LITERAL default user into uvicorn's
 * env. Literals only, never the plugin's configured credentials: all Cognee
 * plugins share one local server and database, and cognee sets the default
 * user's password once, so a per-plugin user here would let whichever plugin
 * boots first define the default user for everyone. "Set only if absent" keeps
 * an operator's own DEFAULT_USER_* export in charge.
 */

import { ENSURE_SCRIPT_CONTENT } from "../../src/server";

describe("ensure_and_boot.py default-user env", () => {
  it("setdefaults DEFAULT_USER_PASSWORD and DEFAULT_USER_EMAIL for uvicorn", () => {
    expect(ENSURE_SCRIPT_CONTENT).toContain(
      "env.setdefault('DEFAULT_USER_PASSWORD', 'default_password')",
    );
    expect(ENSURE_SCRIPT_CONTENT).toContain(
      "env.setdefault('DEFAULT_USER_EMAIL', 'default_user@example.com')",
    );
  });

  it("sets them on the env handed to uvicorn, after copying the process env", () => {
    // setdefault only honours an operator override if the copy happens first.
    const copyAt = ENSURE_SCRIPT_CONTENT.indexOf("env = dict(os.environ)");
    const passwordAt = ENSURE_SCRIPT_CONTENT.indexOf("env.setdefault('DEFAULT_USER_PASSWORD'");
    const spawnAt = ENSURE_SCRIPT_CONTENT.indexOf("'cognee.api.client:app'");
    expect(copyAt).toBeGreaterThan(-1);
    expect(passwordAt).toBeGreaterThan(copyAt);
    expect(spawnAt).toBeGreaterThan(passwordAt);
  });

  it("never overwrites with assignment (the operator's export must win)", () => {
    expect(ENSURE_SCRIPT_CONTENT).not.toMatch(/env\['DEFAULT_USER_(PASSWORD|EMAIL)'\]\s*=/);
  });
});
