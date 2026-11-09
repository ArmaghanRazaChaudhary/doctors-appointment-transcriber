import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = { title: 'Suno — Clinical workspace', description: 'A synthetic-data demo of assisted consultation workflows for clinics and hospitals.' };
export default function RootLayout({children}: Readonly<{children: React.ReactNode}>) { return <html lang="en"><body>{children}</body></html>; }
