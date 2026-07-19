import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
    title: "Document Analyser",
    description:
        "Chat over the document-analysis pipeline: streamed, cited answers with visible guardrail actions.",
};

export default function RootLayout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    return (
        <html lang="en">
            <body className="antialiased">{children}</body>
        </html>
    );
}
