import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function MetricsView() {
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">CPU Usage</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-48 rounded-md border border-dashed bg-muted/20 flex items-center justify-center text-sm text-muted-foreground">
            Chart rendering coming soon...
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Memory Usage</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-48 rounded-md border border-dashed bg-muted/20 flex items-center justify-center text-sm text-muted-foreground">
            Chart rendering coming soon...
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
