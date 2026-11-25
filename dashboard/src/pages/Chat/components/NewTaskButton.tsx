import { Dropdown } from "antd";
import { MessageSquarePlus } from "lucide-react";
import { useTranslation } from "react-i18next";

interface NewTaskButtonProps {
  className: string;
  size?: number;
  disabled?: boolean;
  onNewChat: () => void;
  onNewCloudTask?: () => void;
}

export default function NewTaskButton({
  className,
  size = 14,
  disabled,
  onNewChat,
  onNewCloudTask,
}: NewTaskButtonProps) {
  const { t } = useTranslation();
  const label = t("chatWelcome.newChat");
  const button = (
    <button
      type="button"
      className={className}
      disabled={disabled}
      aria-label={label}
      aria-haspopup={onNewCloudTask ? "menu" : undefined}
      title={label}
      onKeyDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        if (!onNewCloudTask) onNewChat();
      }}
    >
      <MessageSquarePlus size={size} strokeWidth={1.75} aria-hidden />
    </button>
  );
  if (!onNewCloudTask) return button;
  return (
    <Dropdown
      disabled={disabled}
      trigger={["click"]}
      menu={{
        onClick: ({ domEvent }) => domEvent.stopPropagation(),
        items: [
          {
            key: "server",
            label: t("chat.newCloudTask"),
            onClick: onNewCloudTask,
          },
          { key: "local", label: t("chat.newLocalTask"), onClick: onNewChat },
        ],
      }}
    >
      {button}
    </Dropdown>
  );
}
