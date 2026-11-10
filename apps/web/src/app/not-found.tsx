import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-4 p-6 text-center">
      <p className="font-sfx text-5xl text-accent">Huh?</p>
      <h1 className="text-2xl font-bold">We can&apos;t find that page</h1>
      <Link href="/dashboard" className="font-semibold underline underline-offset-4">Back to your comics</Link>
    </main>
  );
}
