import { isCompanyDesktop } from "./desktopRuntime";

type NativeSaveResult = { path?: string; error?: string };

declare global {
  interface Window {
    __mossworkSaveBlobResult?: (
      requestId: string,
      result: NativeSaveResult,
    ) => void;
    webkit?: {
      messageHandlers?: {
        external?: { postMessage: (message: string) => void };
      };
    };
    chrome?: {
      webview?: { postMessage: (message: string) => void };
    };
  }
}

/**
 * Save a downloaded blob through the desktop shell's native save dialog.
 * Returns the selected path, null when the user cancels, and undefined when
 * running in an ordinary browser without the desktop bridge.
 */
export async function saveBlobWithNativeDialog(
  blob: Blob,
  filename: string,
): Promise<string | null | undefined> {
  if (!isCompanyDesktop()) return undefined;

  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(
      ...bytes.subarray(offset, offset + chunkSize),
    );
  }
  const encoded = btoa(binary);
  const requestId =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
          value.toString(16).padStart(2, "0"),
        ).join("");
  const result = await new Promise<NativeSaveResult>((resolve, reject) => {
    const previous = window.__mossworkSaveBlobResult;
    window.__mossworkSaveBlobResult = (id, value) => {
      if (id === requestId) {
        window.__mossworkSaveBlobResult = previous;
        resolve(value);
      } else {
        previous?.(id, value);
      }
    };
    const message = JSON.stringify({
      type: "mosswork:save-blob",
      requestId,
      filename,
      encoded,
    });
    try {
      const bridge = window.webkit?.messageHandlers?.external;
      if (bridge?.postMessage) {
        bridge.postMessage(message);
      } else if (window.chrome?.webview?.postMessage) {
        window.chrome.webview.postMessage(message);
      } else {
        window.__mossworkSaveBlobResult = previous;
        reject(new Error("desktop bridge unavailable"));
      }
    } catch (error) {
      window.__mossworkSaveBlobResult = previous;
      reject(error);
    }
  });
  if (result.error) throw new Error(result.error);
  return result.path || null;
}
