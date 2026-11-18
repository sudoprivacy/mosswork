import { useCallback, useEffect, useRef, useState, type Key } from "react";
import { Button, Spin, Tree, type TreeDataNode } from "antd";
import { FolderOpen, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { isCompanyDesktop } from "../../../api/desktopRuntime";
import {
  mossworkThreadsApi,
  type ThreadArtifact,
  type ThreadDirectoryEntry,
  type ThreadWorkspace,
} from "../../../api/modules/mossworkThreads";
import { getAuthToken } from "../../../api/request";
import { fileTreeIcon } from "../../../utils/fileTreeIcon";
import { dockFileBasename } from "../utils/dockFilePath";
import styles from "../index.module.less";

interface Props {
  agentId: string;
  threadId: string;
  streaming: boolean;
  onOpenFile: (path: string, agentId?: string | null) => void;
}

interface WorkspaceNode extends TreeDataNode {
  legacyFile?: ThreadArtifact;
}

// Directory keys are relative paths, so this group cannot collide with a folder.
const legacyGroup = "/legacy";

export default function ThreadWorkspacePanel(props: Props) {
  const [revision, setRevision] = useState(0);
  const expanded = useRef<Key[]>([legacyGroup]);

  // Reload the root and its expanded branches together after a turn or refresh.
  return (
    <WorkspaceTree
      key={JSON.stringify([
        props.agentId,
        props.threadId,
        props.streaming,
        revision,
      ])}
      {...props}
      expanded={expanded}
      onRefresh={() => setRevision((n) => n + 1)}
    />
  );
}

function WorkspaceTree({
  agentId,
  threadId,
  onOpenFile,
  onRefresh,
  expanded,
}: Omit<Props, "streaming"> & {
  onRefresh: () => void;
  expanded: { current: Key[] };
}) {
  const { t } = useTranslation();
  const [workspace, setWorkspace] = useState<ThreadWorkspace | null>(null);
  const [directories, setDirectories] = useState(
    () => new Map<string, ThreadDirectoryEntry[]>(),
  );
  const [error, setError] = useState("");
  const desktopRequired = t("chat.threadDirectory.desktopRequired");
  const pending = t("chat.threadDirectory.pending");
  const readFailed = t("workspace.readFailed");

  const nativeDirectory = useCallback(
    async (path: string, reveal = false) => {
      const bridge = window.__mossworkRuntime;
      if (!bridge?.threadWorkspace) throw new Error(desktopRequired);
      return bridge.threadWorkspace({
        agent_id: agentId,
        thread_id: threadId,
        token: getAuthToken(),
        path,
        reveal,
      });
    },
    [agentId, threadId, desktopRequired],
  );

  useEffect(() => {
    let current = true;
    async function load() {
      const root = await mossworkThreadsApi.workspace(agentId, threadId, "");
      if (!current) return;
      if (root.location === "desktop") {
        const local = await nativeDirectory("");
        if (!local) throw new Error(readFailed);
        if (current) setWorkspace({ ...root, entries: local.entries });
      } else if (!root.has_messages && isCompanyDesktop()) {
        throw new Error(pending);
      } else {
        setWorkspace(root);
      }
    }
    void load().catch((err: Error) => {
      if (current) setError(err.message);
    });
    return () => {
      current = false;
    };
  }, [agentId, threadId, nativeDirectory, readFailed, pending]);

  function reveal(path: string) {
    void nativeDirectory(path, true).catch((err: Error) =>
      setError(err.message),
    );
  }

  function treeNodes(
    entries: ThreadDirectoryEntry[],
    parent: string,
  ): WorkspaceNode[] {
    return [...entries]
      .sort(
        (a, b) =>
          Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name),
      )
      .map((entry) => {
        const path = parent ? `${parent}/${entry.name}` : entry.name;
        const children = directories.get(path);
        return {
          key: path,
          title: entry.name,
          isLeaf: !entry.is_dir,
          icon: entry.is_dir ? undefined : fileTreeIcon(entry.name),
          children: children && treeNodes(children, path),
        };
      });
  }

  const nodes = treeNodes(workspace?.entries ?? [], "");
  if (workspace?.legacy_files.length) {
    nodes.push({
      key: legacyGroup,
      title: t("chat.threadDirectory.legacy"),
      children: workspace.legacy_files.map((file, index) => ({
        key: `${legacyGroup}/${index}`,
        title: dockFileBasename(file.path),
        isLeaf: true,
        icon: fileTreeIcon(file.path),
        legacyFile: file,
      })),
    });
  }

  return (
    <section
      className={styles.threadWorkspace}
      aria-label={t("chat.openWorkspace")}
    >
      <div className={styles.threadWorkspaceToolbar}>
        {workspace && (
          <strong>{t(`chat.threadDirectory.${workspace.location}`)}</strong>
        )}
        <Button
          type="text"
          aria-label={t("common.refresh")}
          onClick={onRefresh}
          icon={<RefreshCw size={16} />}
        />
        {workspace?.location === "desktop" && (
          <Button
            type="text"
            onClick={() => reveal("")}
            icon={<FolderOpen size={16} />}
          >
            {t("chat.threadDirectory.reveal")}
          </Button>
        )}
      </div>
      {error && <div role="status">{error}</div>}
      {!workspace && !error && <Spin />}
      {workspace &&
        (nodes.length === 0 ? (
          <div>{t("cos.noFiles")}</div>
        ) : (
          <Tree.DirectoryTree<WorkspaceNode>
            aria-label={t("chat.openWorkspace")}
            showLine={{ showLeafIcon: false }}
            treeData={nodes}
            defaultExpandedKeys={expanded.current}
            onExpand={(keys) => {
              expanded.current = keys;
            }}
            loadData={async (node) => {
              const path = String(node.key);
              if (path === legacyGroup) return;
              try {
                const listing =
                  workspace.location === "desktop"
                    ? await nativeDirectory(path)
                    : await mossworkThreadsApi.workspace(
                        agentId,
                        threadId,
                        path,
                      );
                if (!listing) throw new Error(readFailed);
                setDirectories((prev) =>
                  new Map(prev).set(path, listing.entries),
                );
              } catch (err) {
                setError(err instanceof Error ? err.message : readFailed);
              }
            }}
            onSelect={(_, { node }) => {
              if (!node.isLeaf) return;
              if (node.legacyFile) {
                onOpenFile(node.legacyFile.path, node.legacyFile.agent_id);
              } else if (workspace.location === "desktop") {
                reveal(String(node.key));
              } else {
                onOpenFile(`${workspace.root}/${node.key}`, agentId);
              }
            }}
          />
        ))}
    </section>
  );
}
