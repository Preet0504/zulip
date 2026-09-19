let is_recap_visible = false;

export function set_visible(value: boolean): void {
    is_recap_visible = value;
}

export function is_visible(): boolean {
    return is_recap_visible;
}
