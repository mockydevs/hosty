import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function TerminalTab() {
  return (
    <Card className="bg-black text-green-500 border-border/50 shadow-md">
      <CardHeader className="border-b border-border/30 pb-2">
        <CardTitle className="text-base font-mono text-gray-400">Web Terminal</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="h-96 p-4 font-mono text-sm overflow-hidden flex items-start">
          <span>root@container:~# <span className="animate-pulse">_</span></span>
        </div>
      </CardContent>
    </Card>
  );
}
