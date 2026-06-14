import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

export function ResourceLimitsConfig() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Resource Limits</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>CPU Limit (Cores)</Label>
          <div className="flex gap-2">
            <Input placeholder="e.g. 1.5" />
          </div>
        </div>
        <div className="grid gap-2">
          <Label>Memory Limit (MB)</Label>
          <div className="flex gap-2">
            <Input placeholder="e.g. 1024" />
          </div>
        </div>
        <Button className="mt-2">Save Limits</Button>
      </CardContent>
    </Card>
  );
}
