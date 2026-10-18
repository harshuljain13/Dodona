import "./globals.css";
import "@livekit/components-styles";
import type { ReactNode } from "react";

export const metadata = {
  title: "Dodona — voice deep research",
  description: "A self-hosted research agent, answered by one GPU.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
