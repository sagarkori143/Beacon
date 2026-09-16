import type { Metadata } from "next";

import ToastProvider from "@/components/Toast";

import "./globals.css";

export const metadata: Metadata = {
  title: "Beacon",
  description: "Ask any company here about its own policies, hours and branches.",
};

/** Applies the saved theme before first paint, so there is no light flash. */
const THEME_BOOT = `(function(){try{var t=localStorage.getItem('beacon-theme');
if(t==='light')document.documentElement.setAttribute('data-theme','light');}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark">
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
