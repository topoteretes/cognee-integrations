/**
 * Jest config for the opt-in integration smoke test.
 *
 * Tests live under __tests__/ and are compiled by ts-jest with a dedicated
 * tsconfig (tsconfig.jest.json) so they stay OUT of the production tsconfig's
 * `include` (credentials/**, nodes/**). That keeps the CI `tsc --noEmit` step —
 * which type-checks the shipped node — completely unaffected by test code.
 */
/** @type {import('jest').Config} */
module.exports = {
  testEnvironment: 'node',
  testMatch: ['**/__tests__/**/*.test.ts'],
  transform: {
    '^.+\\.ts$': ['ts-jest', { tsconfig: 'tsconfig.jest.json' }],
  },
  // A live cognify on Cognee Cloud can take minutes; give the smoke room.
  testTimeout: 900000,
};