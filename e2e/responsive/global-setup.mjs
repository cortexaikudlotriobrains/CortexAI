import path from "node:path";
import { pathToFileURL } from "node:url";

export default async function globalSetup(config) {
    const e2eRoot = path.resolve(import.meta.dirname, "..");
    const repoRoot = path.resolve(e2eRoot, "..");
    const frontendRoot = path.join(repoRoot, "frontend-react");
    const viteModuleUrl = pathToFileURL(
        path.join(frontendRoot, "node_modules", "vite", "dist", "node", "index.js"),
    ).href;
    const { createServer } = await import(viteModuleUrl);
    const port = Number(config.metadata.responsivePort);
    const server = await createServer({
        root: frontendRoot,
        // Independent responsive servers must not race Vite's deps rename on Windows.
        cacheDir: path.join(repoRoot, ".runtime", "e2e-vite-cache", String(port)),
        configFile: path.join(frontendRoot, "vite.config.ts"),
        logLevel: "error",
        server: {
            host: "127.0.0.1",
            port,
            strictPort: true,
        },
    });

    await server.listen();
    return async () => {
        await server.close();
    };
}
