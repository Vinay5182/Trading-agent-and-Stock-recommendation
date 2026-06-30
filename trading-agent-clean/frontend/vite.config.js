import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: process.env.VITE_HOST || "127.0.0.1",
    port: parseInt(process.env.VITE_PORT || "5173", 10),
  },
  preview: {
    host: process.env.VITE_HOST || "127.0.0.1",
    port: parseInt(process.env.VITE_PORT || "5173", 10),
  }
});
