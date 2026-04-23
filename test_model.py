from mlx_lm import load, generate

MODEL_NAME = "mlx-community/Qwen2.5-7B-Instruct-4bit"
# MODEL_NAME = "mlx-community/Qwen3.5-9B-4bit"

def main():
    print(f"Loading model: {MODEL_NAME}")

    model, tokenizer = load(MODEL_NAME)

    messages = [
        {
            "role": "system",
            "content": "你是一个简洁、自然的中文 Mac 系统助手。默认不会自动执行危险操作。"
        },
        {
            "role": "user",
            "content": "如果我说‘开工’，你觉得这可能对应哪些电脑操作？"
        },
    ]

    prompt = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
    )

    response = generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=200,
    )

    print("\n=== RESPONSE ===")
    print(response)

if __name__ == "__main__":
    main()