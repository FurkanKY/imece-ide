/* ErrorBoundary — Beta-2: bir bölge çökerse IDE'nin kalanı yaşar.
   Kök + AI paneli ayrı sınırlarla sarılır; fallback: yeniden dene + sorun bildir. */

import { Component, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { bridge } from "@/bridge";
import { logError } from "@/lib/errlog";
import { Button, EmptyState } from "@/components/ui";

const ISSUES_URL = "https://github.com/FurkanKY/multi-agent/issues/new";

interface Props {
  /** fallback başlığında görünen bölge adı (ör. "AI paneli") */
  label: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    logError(`${this.props.label} çöktü: ${error.message}`, error.stack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <EmptyState
        icon={AlertTriangle}
        title={`${this.props.label} bir hatayla karşılaştı`}
        description={`${this.state.error.message} — kaydedilmemiş işiniz güvende; bu bölgeyi yeniden deneyebilirsiniz.`}
        className="h-full"
        action={
          <div className="flex gap-2">
            <Button size="sm" variant="primary" onClick={() => this.setState({ error: null })}>
              Yeniden Dene
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void bridge.call("app.openExternal", { url: ISSUES_URL })}
            >
              Sorunu Bildir
            </Button>
          </div>
        }
      />
    );
  }
}
