import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const frontendPort = Number(
    process.env.FRONTEND_PORT || env.FRONTEND_PORT || 5173,
  );
  const copilotRuntimeTarget =
    process.env.VITE_COPILOTKIT_RUNTIME_PROXY ||
    env.VITE_COPILOTKIT_RUNTIME_PROXY ||
    "http://127.0.0.1:4000";

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: frontendPort,
      proxy: {
        "/api/copilotkit": {
          target: copilotRuntimeTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
