import { useCallback, useEffect, useRef, useState } from "react";
import type { CanonicalIdentity } from "../api/modules/canonicalIdentity";
import {
  canonicalOrganizationApi,
  type CanonicalDepartment,
  type CanonicalInvitation,
  type CanonicalOrganizationMember,
  type DepartmentChanges,
} from "../api/modules/canonicalOrganization";
import { getAuthToken } from "../api/request";

interface Directory {
  readonly members: CanonicalOrganizationMember[];
  readonly departments: CanonicalDepartment[];
}
type Change =
  | {
      readonly kind: "create";
      readonly input: {
        readonly name: string;
        readonly parent_id: string | null;
        readonly external_id?: string | null;
      };
    }
  | {
      readonly kind: "update";
      readonly id: string;
      readonly changes: DepartmentChanges;
    }
  | { readonly kind: "delete"; readonly id: string }
  | {
      readonly kind: "password";
      readonly id: string;
      readonly password: string;
    }
  | { readonly kind: "add"; readonly phone: string };

/** Display ceilings only. Every HTTP action still requires the actual server grants. */
export const showsFullOrganizationAdmin = (role: string) =>
  role === "admin" || role === "super_admin";
export const showsOrganizationDirectory = (role: string) =>
  showsFullOrganizationAdmin(role) || role === "dept_admin";

export function useCanonicalOrganization(identity: CanonicalIdentity) {
  const actor = JSON.stringify([
    identity.account_id,
    identity.organization.id,
    identity.member.id,
  ]);
  const currentActor = useRef(actor);
  currentActor.current = actor;
  const readGeneration = useRef(0);
  const inviteGeneration = useRef(0);
  const readAbort = useRef<AbortController | null>(null);
  const inviteAbort = useRef<AbortController | null>(null);
  const bound = useRef<{
    readonly actor: string;
    readonly token: string;
    readonly data: Directory;
  } | null>(null);
  const writePending = useRef(false);
  const [data, setData] = useState<Directory | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [writeUnconfirmed, setWriteUnconfirmed] = useState(false);
  const [invitations, setInvitations] = useState<{
    readonly items: CanonicalInvitation[];
    readonly total: number;
  } | null>(null);
  const [invitesLoading, setInvitesLoading] = useState(false);
  const [inviteError, setInviteError] = useState<unknown>(null);
  const access = getAuthToken();

  const reload = useCallback(async (): Promise<boolean> => {
    const sequence = ++readGeneration.current;
    inviteGeneration.current++;
    readAbort.current?.abort();
    inviteAbort.current?.abort();
    const controller = new AbortController();
    readAbort.current = controller;
    const token = getAuthToken();
    bound.current = null;
    setData(null);
    setInvitations(null);
    setInviteError(null);
    setInvitesLoading(false);
    setError(null);
    setWriteUnconfirmed(false);
    setIsLoading(Boolean(token));
    const fresh = () =>
      sequence === readGeneration.current &&
      actor === currentActor.current &&
      token === getAuthToken();
    if (!token) return false;
    try {
      const [members, departments] = await Promise.all([
        canonicalOrganizationApi.members(token, controller.signal),
        canonicalOrganizationApi.departments(token, controller.signal),
      ]);
      if (!fresh()) return false;
      if (
        [...members, ...departments].some(
          (row) => row.organization_id !== identity.organization.id,
        ) ||
        new Set(members.map((row) => row.member_id)).size !== members.length ||
        new Set(departments.map((row) => row.department_id)).size !==
          departments.length
      )
        throw new Error("canonicalOrganization.readUnavailable");
      const directory = { members, departments };
      bound.current = { actor, token, data: directory };
      setData(directory);
      return true;
    } catch (reason) {
      if (fresh()) setError(reason);
      return false;
    } finally {
      if (fresh()) setIsLoading(false);
    }
  }, [actor, identity.organization.id]);

  const withdrawReads = useCallback(() => {
    readGeneration.current++;
    inviteGeneration.current++;
    bound.current = null;
    readAbort.current?.abort();
    inviteAbort.current?.abort();
  }, []);

  useEffect(() => {
    void reload();
    return withdrawReads;
  }, [reload, access, withdrawReads]);

  const loadInvitations = async (options: {
    readonly status?: CanonicalInvitation["status"];
    readonly offset: number;
    readonly limit: number;
  }): Promise<boolean> => {
    const token = getAuthToken();
    if (
      !token ||
      actor !== bound.current?.actor ||
      token !== bound.current?.token ||
      actor !== currentActor.current ||
      writePending.current
    )
      return false;
    const sequence = ++inviteGeneration.current;
    inviteAbort.current?.abort();
    const controller = new AbortController();
    inviteAbort.current = controller;
    setInvitations(null);
    setInviteError(null);
    setInvitesLoading(true);
    const fresh = () =>
      sequence === inviteGeneration.current &&
      actor === currentActor.current &&
      token === getAuthToken();
    try {
      const result = await canonicalOrganizationApi.invitations(
        options,
        token,
        controller.signal,
      );
      if (!fresh()) return false;
      if (
        result.items.some(
          (row) => row.organization_id !== identity.organization.id,
        ) ||
        new Set(result.items.map((row) => row.invitation_id)).size !==
          result.items.length
      )
        throw new Error("canonicalOrganization.readUnavailable");
      setInvitations(result);
      return true;
    } catch (reason) {
      if (fresh()) setInviteError(reason);
      return false;
    } finally {
      if (fresh()) setInvitesLoading(false);
    }
  };

  const mutate = async (change: Change): Promise<boolean> => {
    const held = bound.current;
    const token = getAuthToken();
    if (
      !held ||
      actor !== held.actor ||
      !token ||
      token !== held.token ||
      actor !== currentActor.current ||
      writePending.current
    )
      return false;
    if (
      (change.kind === "password" &&
        !held.data.members.some((row) => row.member_id === change.id)) ||
      ((change.kind === "update" || change.kind === "delete") &&
        !held.data.departments.some((row) => row.department_id === change.id))
    )
      return false;
    writePending.current = true;
    const sequence = ++readGeneration.current;
    inviteGeneration.current++;
    readAbort.current?.abort();
    inviteAbort.current?.abort();
    setIsPending(true);
    setError(null);
    setWriteUnconfirmed(false);
    setInvitations(null);
    setInvitesLoading(false);
    setInviteError(null);
    const fresh = () =>
      sequence === readGeneration.current &&
      actor === currentActor.current &&
      token === getAuthToken();
    try {
      switch (change.kind) {
        case "create": {
          const result = await canonicalOrganizationApi.createDepartment(
            change.input,
            token,
          );
          if (result.organization_id !== identity.organization.id)
            throw new Error("canonicalOrganization.writeUnconfirmed");
          break;
        }
        case "update": {
          const result = await canonicalOrganizationApi.updateDepartment(
            change.id,
            change.changes,
            token,
          );
          if (
            result.organization_id !== identity.organization.id ||
            result.department_id !== change.id
          )
            throw new Error("canonicalOrganization.writeUnconfirmed");
          break;
        }
        case "delete":
          await canonicalOrganizationApi.deleteDepartment(change.id, token);
          break;
        case "password":
          await canonicalOrganizationApi.setMemberPassword(
            change.id,
            change.password,
            token,
          );
          break;
        case "add": {
          const result = await canonicalOrganizationApi.addRegisteredMember(
            change.phone,
            token,
          );
          if (result.member.organization_id !== identity.organization.id)
            throw new Error("canonicalOrganization.writeUnconfirmed");
          break;
        }
      }
      if (!fresh()) return false;
      const confirmed = await reload();
      if (
        !confirmed &&
        actor === currentActor.current &&
        token === getAuthToken()
      )
        setWriteUnconfirmed(true);
      return (
        confirmed && actor === currentActor.current && token === getAuthToken()
      );
    } catch (reason) {
      if (fresh()) {
        setError(reason);
        setWriteUnconfirmed(true);
        bound.current = null;
        setData(null);
      }
      return false;
    } finally {
      writePending.current = false;
      // This hook admits only one write at a time. Its completion may release
      // the UI spinner even after an actor switch; it cannot install old data.
      setIsPending(false);
    }
  };

  return {
    data,
    isLoading,
    isPending,
    error,
    writeUnconfirmed,
    reload,
    invitations,
    invitesLoading,
    inviteError,
    loadInvitations,
    createDepartment: (input: Extract<Change, { kind: "create" }>["input"]) =>
      mutate({ kind: "create", input }),
    updateDepartment: (id: string, changes: DepartmentChanges) =>
      mutate({ kind: "update", id, changes }),
    deleteDepartment: (id: string) => mutate({ kind: "delete", id }),
    setMemberPassword: (id: string, password: string) =>
      mutate({ kind: "password", id, password }),
    addRegisteredMember: (phone: string) => mutate({ kind: "add", phone }),
  };
}
