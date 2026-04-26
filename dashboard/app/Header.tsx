"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  clearSession,
  getStoredUser,
  getStoredWorkspace,
  logout as apiLogout,
  SessionUser,
  SessionWorkspace,
} from "./api";

export default function Header() {
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);

  useEffect(() => {
    setUser(getStoredUser());
    setWorkspace(getStoredWorkspace());
  }, []);

  const onLogout = async () => {
    try {
      await apiLogout();
    } catch {
      clearSession();
    }
    router.push("/login");
  };

  return (
    <header className="flex items-center justify-between mb-6">
      <Link href="/" className="text-2xl font-semibold no-underline text-gray-900">
        Lightsei
      </Link>
      <div className="flex items-center gap-4 text-sm">
        {user ? (
          <>
            <span className="text-gray-700">
              <span className="font-mono">{workspace?.name ?? "—"}</span>
              <span className="text-gray-400 mx-2">·</span>
              {user.email}
            </span>
            <Link href="/account" className="text-blue-600 underline">
              account
            </Link>
            <button
              type="button"
              onClick={onLogout}
              className="text-blue-600 underline"
            >
              log out
            </button>
          </>
        ) : (
          <>
            <Link href="/login" className="text-blue-600 underline">
              log in
            </Link>
            <Link href="/signup" className="text-blue-600 underline">
              sign up
            </Link>
          </>
        )}
        <span className="text-xs text-gray-500">polls every 2s</span>
      </div>
    </header>
  );
}
