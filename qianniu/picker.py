"""Production local file/folder picker, independent of chat computer tools."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('kind', choices=('file', 'folder'))
    args = parser.parse_args()
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        path = (filedialog.askopenfilename(parent=root, title='选择商品配置',
                    filetypes=[('商品配置 JSON', '*.json')]) if args.kind == 'file' else
                filedialog.askdirectory(parent=root, title='选择商品图片目录'))
        print(json.dumps({'path': path or None}, ensure_ascii=False), flush=True)
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
