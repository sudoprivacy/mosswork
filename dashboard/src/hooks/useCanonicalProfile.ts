import { useCallback, useEffect, useRef, useState } from "react";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
  type CanonicalProfile,
} from "../api/modules/canonicalIdentity";
import { getAuthToken } from "../api/request";

type Change =
  | { kind: "nickname"; nickname: string }
  | { kind: "password"; oldPassword: string; newPassword: string }
  | { kind: "avatar"; file: File | null };

function readImage(file: File): Promise<string> {
  if (file.size > 2 * 1024 * 1024)
    return Promise.reject(new Error("canonical.profileImageTooLarge"));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () =>
      reject(new Error("canonical.profileImageUnavailable"));
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new Error("canonical.profileImageUnavailable"));
    reader.readAsDataURL(file);
  });
}

/** UI response binding only; all current human/writer checks stay on the server. */
export function useCanonicalProfile(identity: CanonicalIdentity) {
  const actor = JSON.stringify([
    identity.account_id,
    identity.organization.id,
    identity.member.id,
  ]);
  const currentActor = useRef(actor);
  currentActor.current = actor;
  const generation = useRef(0);
  const readAbort = useRef<AbortController | null>(null);
  const writePending = useRef(false);
  const boundToken = useRef("");
  const boundProfile = useRef<CanonicalProfile | null>(null);
  const [profile, setProfile] = useState<CanonicalProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const access = getAuthToken();

  const reload = useCallback(async () => {
    const sequence = ++generation.current;
    const token = getAuthToken();
    readAbort.current?.abort();
    const controller = new AbortController();
    readAbort.current = controller;
    boundToken.current = "";
    boundProfile.current = null;
    setProfile(null);
    setError(null);
    setIsLoading(Boolean(token));
    setIsPending(false);
    const fresh = () =>
      sequence === generation.current &&
      actor === currentActor.current &&
      token === getAuthToken();
    if (!token) return false;
    try {
      const result = await canonicalIdentityApi.profile(
        token,
        controller.signal,
      );
      if (!fresh()) return false;
      if (
        result.account_id !== identity.account_id ||
        result.organization_id !== identity.organization.id ||
        result.member_id !== identity.member.id ||
        typeof result.login_name !== "string" ||
        typeof result.nickname !== "string" ||
        (result.avatar !== null && typeof result.avatar !== "string") ||
        typeof result.can_change_password !== "boolean"
      )
        throw new Error("canonical.profileUnavailable");
      boundToken.current = token;
      boundProfile.current = result;
      setProfile(result);
      return true;
    } catch (reason) {
      if (fresh()) setError(reason);
      return false;
    } finally {
      if (fresh()) setIsLoading(false);
    }
  }, [
    actor,
    identity.account_id,
    identity.organization.id,
    identity.member.id,
  ]);

  useEffect(() => {
    void reload();
    return () => {
      generation.current++;
      boundToken.current = "";
      boundProfile.current = null;
      readAbort.current?.abort();
    };
  }, [reload, access]);

  const mutate = async (change: Change): Promise<boolean> => {
    // A token replacement must not reuse the previous actor's loaded profile,
    // even in the interval before React handles the credential-change event.
    const token = getAuthToken();
    if (
      writePending.current ||
      !token ||
      token !== boundToken.current ||
      !boundProfile.current ||
      actor !== currentActor.current
    )
      return false;
    writePending.current = true;
    const sequence = ++generation.current;
    const fresh = () =>
      sequence === generation.current &&
      token === getAuthToken() &&
      actor === currentActor.current;
    setIsPending(true);
    setError(null);
    let writeStarted = false;
    try {
      switch (change.kind) {
        case "nickname":
          writeStarted = true;
          await canonicalIdentityApi.updateNickname(change.nickname, token);
          break;
        case "password":
          writeStarted = true;
          await canonicalIdentityApi.changePassword(
            change.oldPassword,
            change.newPassword,
            token,
          );
          break;
        case "avatar": {
          const avatar =
            change.file === null ? null : await readImage(change.file);
          if (!fresh()) return false;
          writeStarted = true;
          await canonicalIdentityApi.updateAvatar(avatar, token);
          break;
        }
      }
      if (!fresh()) return false;
      setIsPending(false);
      // A successful response permits a current read. Errors never replay a write
      // or imply rollback; the user can explicitly reload the original profile.
      const confirmed = await reload();
      return (
        confirmed && token === getAuthToken() && actor === currentActor.current
      );
    } catch (reason) {
      if (fresh()) {
        setError(reason);
        if (writeStarted) {
          // A refusal can arrive after a commit. Require a fresh read before
          // another explicit change; never replay the possible original write.
          boundToken.current = "";
          boundProfile.current = null;
          setProfile(null);
        }
      }
      return false;
    } finally {
      writePending.current = false;
      if (token === getAuthToken() && actor === currentActor.current)
        setIsPending(false);
    }
  };

  return {
    profile,
    isLoading,
    isPending,
    error,
    reload,
    saveNickname: (nickname: string) => mutate({ kind: "nickname", nickname }),
    saveAvatar: (file: File | null) => mutate({ kind: "avatar", file }),
    changePassword: (oldPassword: string, newPassword: string) =>
      mutate({ kind: "password", oldPassword, newPassword }),
  };
}
