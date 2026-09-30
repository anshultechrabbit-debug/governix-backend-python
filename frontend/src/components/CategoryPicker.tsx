import { useCategories } from "./domain";
import { Select } from "./ui";

/**
 * Category select. Categories are reference data chosen from the organization's
 * list; the suggested category from AI analysis arrives as `emptyLabel`.
 */
export function CategoryPicker({
  value, onChange, emptyLabel = "Select a category…", id, disabled,
}: { value: string; onChange: (id: string) => void; emptyLabel?: string; id?: string; disabled?: boolean }) {
  const categories = useCategories();
  return (
    <Select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      <option value="">{emptyLabel}</option>
      {categories.data?.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
    </Select>
  );
}
