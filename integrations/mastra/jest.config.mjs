/**
 * ts-jest, ESM mode: this package is "type": "module" (NodeNext resolution,
 * so sources use .js-suffixed relative imports). extensionsToTreatAsEsm and
 * ts-jest/presets/default-esm make Jest run .ts files as native ES modules;
 * this requires Node's --experimental-vm-modules flag, which the
 * test/test:live scripts in package.json already pass.
 */

/** @type {import('jest').Config} */
export default {
  preset: "ts-jest/presets/default-esm",
  testEnvironment: "node",
  extensionsToTreatAsEsm: [".ts"],
  // Rewrites the .js extension NodeNext requires in relative imports back to
  // extension-less specifiers, so ts-jest's resolver finds the .ts source
  // instead of a nonexistent .js file.
  moduleNameMapper: {
    "^(\\.{1,2}/.*)\\.js$": "$1",
  },
  // isolatedModules lives in tsconfig.json, not here — ts-jest 29 deprecates
  // this option and warns (TS151002) if set on both.
  transform: {
    "^.+\\.tsx?$": ["ts-jest", { useESM: true }],
  },
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  // *.test.ts keeps __tests__/test-utils/** (helpers, not specs) out of the
  // suite; testPathIgnorePatterns additionally skips __tests__/live/ so
  // `npm test` never touches it even without the CLI's own ignore flag.
  testMatch: ["<rootDir>/__tests__/**/*.test.ts"],
  testPathIgnorePatterns: ["/node_modules/", "<rootDir>/__tests__/live/"],
  collectCoverageFrom: ["src/**/*.ts"],
  coverageProvider: "v8",
};
