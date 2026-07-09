import { CogneeApi } from "../credentials/CogneeApi.credentials";

describe("CogneeApi", () => {
    it("should expose the credential name", () => {
        const credential = new CogneeApi();

        expect(credential.name).toBe("cogneeApi");
    });
});