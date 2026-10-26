import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "Dodona — voice deep research",
  description: "A self-hosted research agent, answered by one GPU.",
};

// Set the theme BEFORE first paint, and before React hydrates.
//
// Two problems this solves at once, and both are visible:
//
//   the flash — the browser paints the default theme, then React mounts and
//   swaps it. On a projector that white-to-dark jump is the first thing a room
//   sees.
//
//   the hydration mismatch — reading localStorage during render makes the
//   server's HTML and the client's first render disagree, which React reports
//   as an error and resolves by throwing away the server markup.
//
// Running it as a blocking script in <head> means the attribute is already on
// <html> when the first pixel is drawn and when hydration compares trees. It
// has to be inline: an external file would be a second round trip, and the
// flash is exactly as long as that takes.
const SET_THEME = `
(function () {
  try {
    var saved = localStorage.getItem("dodona-theme");
    var system = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", saved || system);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "dark");
  }
})();
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // suppressHydrationWarning on <html> only: the script above mutates this
    // element's attributes before hydration, which React would otherwise
    // report as a mismatch. Scoped to the one element that is legitimately
    // different — anything wider would hide real mismatches underneath.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: SET_THEME }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
